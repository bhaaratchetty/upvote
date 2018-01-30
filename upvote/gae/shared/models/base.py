# Copyright 2017 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Model definitions for Upvote."""
import abc
import datetime
import hashlib
import logging
import random
import re

from google.appengine.api import memcache
from google.appengine.api import users
from google.appengine.ext import ndb
from google.appengine.ext.ndb import polymodel

from upvote.gae.shared.common import settings
from upvote.gae.shared.common import taskqueue_utils
from upvote.gae.shared.common import user_map
from upvote.gae.shared.models import bigquery
from upvote.gae.shared.models import utils as model_utils
from upvote.shared import constants


_ROLLOUT_GROUP_COUNT = 1000
_UNASSIGNED_ROLLOUT_GROUP = -1
_BLACKLIST_MEMCACHE_KEY = '_blacklist'
_BLACKLIST_MEMCACHE_EXPIRATION = 3600  # in seconds


class Error(Exception):
  """Base error for models."""


class UnknownUserError(Error):
  """The current user cannot be accurately determined for some reason."""


class InvalidArgumentError(Error):
  """The called function received an invalid argument."""


class InvalidUserRoleError(Error):
  """The called function received an invalid user role."""


class BaseModelMixin(object):
  """Mix-in for base model common code."""

  def GetPlatformName(self):
    return None

  def GetClientName(self):
    return None

  def to_dict(self, include=None, exclude=None):  # pylint: disable=g-bad-name
    """Convert the model to a dict."""
    result = super(BaseModelMixin, self).to_dict(
        include=include, exclude=exclude)

    if exclude is None or 'id' not in exclude:
      # Check for the key just in case put() hasn't been called yet.
      if hasattr(self, 'key') and self.key is not None:
        result['id'] = self.key.id()
        result['key'] = self.key.urlsafe()

    if exclude is None or 'operating_system_family' not in exclude:
      platform_name = self.GetPlatformName()
      if platform_name:
        result['operating_system_family'] = platform_name

    return result


class Event(BaseModelMixin, polymodel.PolyModel):
  """Blockable Event.

  key = Key(User, user_email) -> Key(Host, host_id) ->
      Key(..., Blockable, hash) -> Key(Event, '1')
  NOTE: The Blockable key may be of any length (e.g. for Bundles).
  NOTE: The Event id is always '1'.

  Attributes:
    blockable_key: key, key to the blockable associated with this event.
    cert_key: key, key to the cert associated with this event.
    host_id: str, unique ID for the host on which this event occurred.
    file_name: str, filename of the blockable on last block.
    file_path: str, path of the blockable on last block.
    publisher: str, publisher of this file.
    version: str, version number of this file.
    executing_user: str, user who executed the binary (may be a system user).
    event_type: str, reason this event was initially created.
    recorded_dt: datetime, when this event was received by the server.
    first_blocked_dt: datetime, time of the first block.
    last_blocked_dt: datetime, time of the last block.
    count: int, the number of times a given event has occurred.
  """
  blockable_key = ndb.KeyProperty()
  cert_key = ndb.KeyProperty()
  file_name = ndb.StringProperty()
  file_path = ndb.StringProperty()
  publisher = ndb.StringProperty()
  version = ndb.StringProperty()

  host_id = ndb.StringProperty()
  executing_user = ndb.StringProperty()
  event_type = ndb.StringProperty(
      choices=constants.EVENT_TYPE.SET_ALL, required=True)

  recorded_dt = ndb.DateTimeProperty(auto_now_add=True)
  first_blocked_dt = ndb.DateTimeProperty()
  last_blocked_dt = ndb.DateTimeProperty()
  count = ndb.IntegerProperty(default=1)

  @property
  def run_by_local_admin(self):
    """Whether the Event was generated by the platform's admin user.

    Due to the platform-specific nature of "admin user," this property should be
    overridden by each platform's derivative models.

    Returns:
      bool, See method description.
    """
    return False

  @property
  def user_key(self):
    if not self.key:
      return None
    return ndb.Key(flat=self.key.pairs()[0])

  def _DedupeEarlierEvent(self, earlier_event):
    """Updates if the related Event occurred earlier than the current one."""
    self.first_blocked_dt = earlier_event.first_blocked_dt
    self.event_type = earlier_event.event_type

  def _DedupeMoreRecentEvent(self, more_recent_event):
    """Updates if the related Event is more recent than the current one."""
    self.last_blocked_dt = more_recent_event.last_blocked_dt
    self.file_name = more_recent_event.file_name
    self.file_path = more_recent_event.file_path
    self.executing_user = more_recent_event.executing_user

  def Dedupe(self, related_event):
    """Updates the current Event state with another, related Event."""
    self.count += related_event.count or 1

    # related_event registered an Event earlier than the earliest recorded date
    if self.first_blocked_dt > related_event.first_blocked_dt:
      self._DedupeEarlierEvent(related_event)

    # related_event registered an Event more recently than the most recent
    # recorded date
    if self.last_blocked_dt < related_event.last_blocked_dt:
      self._DedupeMoreRecentEvent(related_event)

  def GetKeysToInsert(self, logged_in_users, host_owners):
    """Returns the list of keys with which this event should be inserted."""
    if settings.EVENT_CREATION == constants.EVENT_CREATION.EXECUTING_USER:
      if self.run_by_local_admin:
        usernames = logged_in_users
      else:
        usernames = [self.executing_user] if self.executing_user else []
    else:  # HOST_OWNERS
      usernames = host_owners

    emails = [user_map.UsernameToEmail(username) for username in usernames]

    keys = []
    for email in emails:
      key_pairs = [(User, email.lower()), (Host, self.host_id)]
      key_pairs += self.blockable_key.pairs()
      key_pairs += [(Event, '1')]
      keys.append(ndb.Key(pairs=key_pairs))
    return keys

  @classmethod
  def DedupeMultiple(cls, events):
    """Dedupes an iterable of new-style Events.

    Args:
      events: An iterable of new-style Event entities to be deduped.

    Returns:
      A list of deduped Events.
    """
    distinct_events = {}
    for event in events:
      duped_event = distinct_events.get(event.key)
      if duped_event:
        duped_event.Dedupe(event)
      else:
        distinct_events[event.key] = event
    return distinct_events.values()

  def to_dict(self, include=None, exclude=None):  # pylint: disable=g-bad-name
    result = super(Event, self).to_dict(include=include, exclude=exclude)
    result['blockable_id'] = self.blockable_key.id()
    return result


class Note(polymodel.PolyModel):
  """An entity used for annotating other entities.

  Attributes:
    message: The text of the note.
    author: The username of this note's author.
    changelists: Integer list of relevant changelist IDs.
    bugs: Integer list of relevant bug IDs.
    tickets: Integer list of relevant ticket IDs.
  """
  message = ndb.TextProperty()
  author = ndb.StringProperty()
  changelists = ndb.IntegerProperty(repeated=True)
  bugs = ndb.IntegerProperty(repeated=True)
  tickets = ndb.IntegerProperty(repeated=True)
  recorded_dt = ndb.DateTimeProperty(auto_now_add=True)

  @classmethod
  def GenerateKey(cls, message, parent):
    key_hash = hashlib.sha256(message).hexdigest()
    return ndb.Key(Note, key_hash, parent=parent)


class Blockable(BaseModelMixin, polymodel.PolyModel):
  """An entity that has been blocked.

  key = id of blockable file

  Attributes:
    id_type: str, type of the id used as the key.
    blockable_hash: str, the hash of the blockable, may also be the id.
    file_name: str, name of the file this blockable represents.
    publisher: str, name of the publisher of the file.
    product_name: str, Product name.
    version: str, Product version.

    occurred_dt: datetime, when the blockable was first seen.
    updated_dt: datetime, when this blockable was last updated.
    recorded_dt: datetime, when this file was first seen.

    score: int, social-voting score for this blockable.

    flagged: bool, True if a user has flagged this file as potentially unsafe.

    notes: str[], list of notes attached to this blockable.
    state: str, state of this blockable
    state_change_dt: datetime, when the state of this blockable changed.
  """

  def _CalculateScore(self):
    # NOTE: Since the 'score' property is a ComputedProperty, it will
    # be re-computed before every put. Consequently, when a Blockable is put for
    # the first time, we won't see a pre-existing value for 'score'. Here, we
    # avoid the score calculation for newly-created Blockables as they shouldn't
    # have any Votes associated with them and, thus, should have a score of 0.
    if not model_utils.HasValue(self, 'score'):
      return 0

    tally = 0
    votes = self.GetVotes()
    for vote in votes:
      if vote.was_yes_vote:
        tally += vote.weight
      else:
        tally -= vote.weight
    return tally

  id_type = ndb.StringProperty(choices=constants.ID_TYPE.SET_ALL, required=True)
  blockable_hash = ndb.StringProperty()
  file_name = ndb.StringProperty()
  publisher = ndb.StringProperty()
  product_name = ndb.StringProperty()
  version = ndb.StringProperty()

  occurred_dt = ndb.DateTimeProperty()
  updated_dt = ndb.DateTimeProperty(auto_now=True)
  recorded_dt = ndb.DateTimeProperty(auto_now_add=True)

  flagged = ndb.BooleanProperty(default=False)

  notes = ndb.KeyProperty(kind=Note, repeated=True)
  state = ndb.StringProperty(
      choices=constants.STATE.SET_ALL,
      required=True,
      default=constants.STATE.UNTRUSTED)
  state_change_dt = ndb.DateTimeProperty(auto_now_add=True)

  score = ndb.ComputedProperty(_CalculateScore)

  @abc.abstractmethod
  def PersistRow(self, action, timestamp=None):
    """Persists the current blockable with the given action.

    Args:
      action: constants.BLOCK_ACTION, the action to persist.
      timestamp: datetime, time of the persistence.
    """

  def ChangeState(self, new_state):
    """Helper method for changing the state of this Blockable.

    Args:
      new_state: New state value to set.
    """
    old_state = self.state
    self.state = new_state
    self.state_change_dt = datetime.datetime.utcnow()
    self.put()

    self.PersistRow(constants.BLOCK_ACTION.STATE_CHANGE, self.state_change_dt)

    message = 'Blockable %s changed from %s to %s' % (
        self.key.id(), old_state, new_state)
    AuditLog.Create(self, message)

  def GetRules(self, in_effect=True):
    """Queries for all Rules associated with blockable.

    Args:
      in_effect: bool, return only rules that are currently in effect.

    Returns:
      A list of Rules.
    """
    query = Rule.query(ancestor=self.key)
    if in_effect:
      # pylint: disable=g-explicit-bool-comparison
      query = query.filter(Rule.in_effect == True)
      # pylint: enable=g-explicit-bool-comparison
    return query.fetch()

  def GetVotes(self):
    """Queries for all Votes cast for this Blockable.

    Returns:
      A list of cast Votes.
    """
    # pylint: disable=g-explicit-bool-comparison
    return Vote.query(Vote.in_effect == True, ancestor=self.key).fetch()
    # pylint: enable=g-explicit-bool-comparison

  def GetStrongestVote(self):
    """Retrieves the 'strongest' vote cast for this Blockable.

    Intended to help replace the need for the 'social_tag' property of the
    models.Request class.

    Returns:
      The 'strongest' Vote cast for this Blockable (i.e. the Vote whose value
      has the largest magnitude), or None if no Votes have been cast.
    """
    votes = self.GetVotes()
    return max(votes, key=lambda vote: abs(vote.weight)) if votes else None

  def GetEvents(self):
    """Retrieves all Events for this Blockable.

    Intended to help replace the need for the 'request' property of the
    models.BlockEvent class, since DB ReferenceProperties don't appear to exist
    in NDB.

    Returns:
      A list of all Event entities associated with this Blockable.
    """
    return Event.query(Event.blockable_key == self.key).fetch()

  def IsVotingAllowed(self, current_user=None):
    """Method to check if voting is allowed.

    Args:
      current_user: The optional User whose voting privileges should be
          evaluated against this Blockable. If not provided, the current
          AppEngine user will be used instead.

    Returns:
      A (boolean, string) tuple. The boolean indicates whether voting is
      allowed. The string provides an explanation if the boolean is False, and
      will be None otherwise.
    """
    # Even admins can't vote on banned or globally whitelisted blockables.
    if self.state in constants.VOTING_PROHIBITED_REASONS.PROHIBITED_STATES:
      return (False, self.state)

    current_user = current_user or User.GetOrInsert()

    if self.state in constants.STATE.SET_VOTING_ALLOWED_ADMIN_ONLY:
      if current_user.is_admin:
        return (True, None)
      else:
        return (False, constants.VOTING_PROHIBITED_REASONS.ADMIN_ONLY)

    if isinstance(self, Certificate) and not current_user.is_admin:
      return (False, constants.VOTING_PROHIBITED_REASONS.ADMIN_ONLY)

    # At this point the state must be in SET_VOTING_ALLOWED, so just check the
    # permissions of the current user.
    if current_user.HasPermissionTo(constants.PERMISSIONS.FLAG):
      return (True, None)
    else:
      return (
          False, constants.VOTING_PROHIBITED_REASONS.INSUFFICIENT_PERMISSION)

  def ResetState(self):
    """Resets blockable to UNTRUSTED with no votes."""
    self.state = constants.STATE.UNTRUSTED
    self.state_change_dt = datetime.datetime.utcnow()
    self.flagged = False
    self.put()

    self.PersistRow(constants.BLOCK_ACTION.RESET, self.state_change_dt)

  def to_dict(self, include=None, exclude=None):  # pylint: disable=g-bad-name
    if exclude is None: exclude = []
    exclude += ['score']
    result = super(Blockable, self).to_dict(include=include, exclude=exclude)

    # NOTE: This is not ideal but it prevents CalculateScore from being
    # called when serializing Blockables. This will return an inaccurate value
    # if a vote was cast after the Blockable was retrieved but this can be
    # avoided by wrapping the call to to_dict in a transaction.
    result['score'] = model_utils.GetLocalComputedPropertyValue(self, 'score')

    allowed, reason = self.IsVotingAllowed()
    result['is_voting_allowed'] = allowed
    result['voting_prohibited_reason'] = reason
    if not allowed:
      logging.info('Voting on this Blockable is not allowed (%s)', reason)
    return result


class Binary(Blockable):
  """A binary to be blocked.

  Attributes:
    cert_key: The Key to the Certificate entity of the binary's signing cert.
  """
  cert_key = ndb.KeyProperty()

  @property
  def rule_type(self):
    return constants.RULE_TYPE.BINARY

  @property
  def cert_id(self):
    return self.cert_key and self.cert_key.id()

  @classmethod
  def TranslatePropertyQuery(cls, field, value):
    if field == 'cert_id':
      if value:
        cert_key = ndb.Key(Certificate, value).urlsafe()
      else:
        cert_key = None
      return 'cert_key', cert_key
    return field, value

  def to_dict(self, include=None, exclude=None):  # pylint: disable=g-bad-name
    result = super(Binary, self).to_dict(include=include, exclude=exclude)
    result['cert_id'] = self.cert_id
    return result


class Certificate(Blockable):

  @property
  def rule_type(self):
    return constants.RULE_TYPE.CERTIFICATE


class Package(Blockable):

  @property
  def rule_type(self):
    return constants.RULE_TYPE.PACKAGE


class Host(BaseModelMixin, polymodel.PolyModel):
  """A device running client software and has interacted with Upvote.

  key = Device UUID reported by client.

  Attributes:
    hostname: str, the hostname at last preflight.
    recorded_dt: datetime, time of insertion.
    hidden: boolean, whether the host will be hidden from the user by default.
  """
  hostname = ndb.StringProperty()
  recorded_dt = ndb.DateTimeProperty(auto_now_add=True)
  hidden = ndb.BooleanProperty(default=False)

  @classmethod
  def GetAssociatedHostIds(cls, user):
    """Returns the IDs of each host which is associated with the given user.

    NOTE: What consitutes "associated with" is platform-dependent and should be
    defined for each inheriting class.

    Args:
      user: User, The user for which associated hosts should be fetched.

    Returns:
      list of str, A list of host IDs for hosts associated with the provided
          user.
    """
    raise NotImplementedError

  def IsAssociatedWithUser(self, user):
    """Returns whether the given user is associated with this host.

    NOTE: What consitutes "associated with" is platform-dependent and should be
    defined for each inheriting class.

    Args:
      user: User, The user whose association will be tested.

    Returns:
      bool, Whether the user is associated with this host.
    """
    raise NotImplementedError

  def GetUserBlockRate(
      self, user, duration_to_fetch=datetime.timedelta(days=60),
      max_events_to_fetch=1000):
    """Calculates the block rate for a given user on this host.

    "Block rate" is defined as the number of _unique_ blockables a user runs on
    the host every _workday_ (i.e. 5 out of 7 days per week).

    Args:
      user: User, The user for whom to calculate the block rate on this
          host.
      duration_to_fetch: datetime.timedelta, The span of time over which the
          block rate should be calculated.
      max_events_to_fetch: int, The maximum number of events to be counted. The
          mitigates the risk that a host with thousands of events results in the
          datastore query timing out.

    Returns:
      (bool, float), A 2-tuple of the form (was_max, block_rate). was_max is
      True when max_events_to_fetch events were found in the provided time
      frame. block_rate is the block rate for the given user on this host.

    Raises:
      InvalidArgumentError: duration_to_fetch is less than 1 day or
          max_events_to_fetch is less than 1.
    """
    # Duration must be at least 1 day.
    if duration_to_fetch.days == 0:
      raise InvalidArgumentError('Duration must be at least 1 day')
    elif max_events_to_fetch <= 0:
      raise InvalidArgumentError('Max Events must be at least 1')

    threshold_dt = datetime.datetime.utcnow() - duration_to_fetch
    parent_key = model_utils.ConcatenateKeys(user.key, self.key)
    query = Event.query(
        Event.last_blocked_dt >= threshold_dt,
        ancestor=parent_key
    ).order(-Event.last_blocked_dt)

    num_events = query.count(limit=max_events_to_fetch)

    was_max = num_events == max_events_to_fetch

    # 5 workdays out of 7 days of the week.
    ratio_of_workdays = 5. / 7
    workdays_to_fetch = ratio_of_workdays * duration_to_fetch.days
    block_rate = float(num_events) / workdays_to_fetch
    return (was_max, block_rate)

  @staticmethod
  def NormalizeId(host_id):
    return host_id.upper()


class Vote(BaseModelMixin, ndb.Model):
  """An individual vote on a blockable cast by a user.

  key = Key(Blockable, hash) -> Key(User, email) -> Key(Vote, 'InEffect')

  Attributes:
    user_email: str, the email of the voting user at the time of the vote.
    was_yes_vote: boolean, True if the vote was "Yes."
    recorded_dt: DateTime, time of vote.
    value: Int, the value of the vote at the time of voting, based on the value
        of the users vote.
    candidate_type: str, the type of blockable being voted on.
    blockable_key: Key, the key of the blockable being voted on.
    in_effect: boolean, True if the vote counts towards the blockable score.
  """
  _IN_EFFECT_KEY_NAME = 'InEffect'

  def _ComputeBlockableKey(self):
    if not self.key:
      return None
    pairs = self.key.pairs()
    if len(pairs) < 3:
      return None
    return ndb.Key(pairs=pairs[:-2])

  user_email = ndb.StringProperty(required=True)
  was_yes_vote = ndb.BooleanProperty(required=True, default=True)
  recorded_dt = ndb.DateTimeProperty(auto_now_add=True)
  weight = ndb.IntegerProperty(default=0)
  candidate_type = ndb.StringProperty(
      choices=constants.RULE_TYPE.SET_ALL, required=True)
  blockable_key = ndb.ComputedProperty(_ComputeBlockableKey)
  in_effect = ndb.ComputedProperty(
      lambda self: self.key and self.key.flat()[-1] == Vote._IN_EFFECT_KEY_NAME)

  @classmethod
  def GetKey(cls, blockable_key, user_key, in_effect=True):
    # In the in_effect == False case, the None ID field of the key will cause
    # NDB to generate a random one when the vote is put.
    vote_id = Vote._IN_EFFECT_KEY_NAME if in_effect else None
    return model_utils.ConcatenateKeys(
        blockable_key, user_key, ndb.Key(Vote, vote_id))

  @property
  def effective_weight(self):
    return self.weight if self.was_yes_vote else -self.weight

  @property
  def user_key(self):
    return ndb.Key(User, self.user_email.lower())


class Rule(BaseModelMixin, polymodel.PolyModel):
  """A rule generated from voting or manually inserted by an authorized user.

  Attributes:
    rule_type: string, the type of blockable the rule applies to, ie
        binary, certificate.
    policy: string, the assertion of the rule, ie whitelisted, blacklisted.
    in_effect: bool, is this rule still in effect. Set to False when superceded.
    recorded_dt: datetime, insertion time.
    host_id: str, id of the host or blank for global.
    user_key: key, for locally scoped rules, the user for whom the rule was
        created.
  """
  rule_type = ndb.StringProperty(
      choices=constants.RULE_TYPE.SET_ALL, required=True)
  policy = ndb.StringProperty(
      choices=constants.RULE_POLICY.SET_ALL, required=True)
  in_effect = ndb.BooleanProperty(default=True)
  recorded_dt = ndb.DateTimeProperty(auto_now_add=True)
  updated_dt = ndb.DateTimeProperty(auto_now=True)
  host_id = ndb.StringProperty(default='')
  user_key = ndb.KeyProperty()

  def MarkDisabled(self):
    self.in_effect = False


def _ValidateRolloutGroup(unused_prop, value):
  if ((value < 0 or value >= _ROLLOUT_GROUP_COUNT) and
      value != _UNASSIGNED_ROLLOUT_GROUP):
    raise ValueError('Invalid rollout group: %d' % value)


class User(BaseModelMixin, ndb.Model):
  """Represents a user in Upvote for voting purposes.

  Tracks the reputation of a single user to determine how much weight their
  votes are worth. Endorsing malware reduces a user's reputation and as a
  result the value of their votes.

  key = user email

  Attributes:
    recorded_dt: datetime, time of insertion.
    vote_weight: int, the weight of their votes.
    roles: string, all roles for current user, i.e. TRUSTED_USER, SECURITY, etc.
    last_vote_dt: datetime, last time this user voted.
    rollout_group: int, a random integer in the range [0, _ROLLOUT_GROUP_COUNT)
        assigned at creation-time.
  """
  _PERMISSION_PROPERTIES = {
      constants.SYSTEM.BIT9: 'bit9_perms',
      constants.SYSTEM.SANTA: 'santa_perms',
  }

  recorded_dt = ndb.DateTimeProperty(auto_now_add=True)
  vote_weight = ndb.IntegerProperty(default=1)
  roles = ndb.StringProperty(repeated=True, choices=constants.USER_ROLE.SET_ALL)
  last_vote_dt = ndb.DateTimeProperty(default=None)
  rollout_group = ndb.IntegerProperty(
      required=True, validator=_ValidateRolloutGroup,
      default=_UNASSIGNED_ROLLOUT_GROUP)

  @classmethod
  @ndb.transactional
  @taskqueue_utils.GroupTransactionalDefers
  def _InnerGetOrInsert(cls, email_addr):
    email_addr = email_addr.lower()
    user = cls.get_by_id(email_addr)
    if user is None:
      initial_roles = [constants.USER_ROLE.USER]
      user = cls(id=email_addr, roles=initial_roles)
      user.AssignRolloutGroup()
      user.put()

      bigquery.UserRow.DeferCreate(
          email=email_addr,
          timestamp=datetime.datetime.utcnow(),
          action=constants.USER_ACTION.FIRST_SEEN,
          roles=initial_roles)
    return user

  @classmethod
  def GetOrInsert(cls, email_addr=None, appengine_user=None):
    """Creates a new User, or retrieves an existing one.

    NOTE: Use this anywhere you would otherwise do an __init__() and put(). We
    need to ensure that the roles Property gets initialized for new users, but
    can't specify a default value.

    Args:
      email_addr: Optional email address string to create the User from.
      appengine_user: Optional AppEngine User to create the User from.

    Returns:
      The User instance.

    Raises:
      UnknownUserError: The current user cannot be determined via either email
          address or AppEngine user.
    """
    # Ultimately, we need an email address. If one isn't specified, fall back
    # to the logged in AppEngine user.
    if email_addr is None:
      appengine_user = appengine_user or users.get_current_user()

      # If we can't fall back to an AppEngine user for some reason, bail.
      if appengine_user is None:
        raise UnknownUserError

      email_addr = appengine_user.email()

    # Do a simple get to see if an User entity already exists for this
    # user. Otherwise, incur a transaction in order to create a new one.
    return cls.GetById(email_addr) or cls._InnerGetOrInsert(email_addr)

  @classmethod
  def GetById(cls, email_addr):
    """Retrieves an existing User.

    NOTE: Use this anywhere you would otherwise do a get_by_id(). We
    need to ensure that the email address is properly tranlated to the internal
    form.

    Args:
      email_addr: The email address string associated with the desired
          User.

    Returns:
      The User instance or None.
    """
    return cls.get_by_id(email_addr.lower())

  @classmethod
  @ndb.transactional(xg=True)  # User and KeyValueCache
  @taskqueue_utils.GroupTransactionalDefers
  def SetRoles(cls, email_addr, new_roles):

    user = User.GetOrInsert(email_addr)
    old_roles = set(user.roles)
    new_roles = set(new_roles)
    all_roles = constants.USER_ROLE.SET_ALL

    # Removing all roles would put this user into a bad state, so don't.
    if not new_roles:
      logging.warning('Cannot remove all roles from user %s', user.nickname)
      return

    # Verify that all the roles provided are valid.
    invalid_roles = new_roles - all_roles
    if invalid_roles:
      raise InvalidUserRoleError(', '.join(invalid_roles))

    # If no role changes are necessary, bail.
    if old_roles == new_roles:
      logging.info('No roles changes necessary for %s', user.nickname)
      return

    # Log the roles changes.
    for role in old_roles - new_roles:
      logging.info('Removing the %s role from %s', role, user.nickname)
    for role in new_roles - old_roles:
      logging.info('Adding the %s role to %s', role, user.nickname)

    # Recalculate the voting weight.
    voting_weights = settings.VOTING_WEIGHTS
    new_vote_weight = max(voting_weights[role] for role in new_roles)
    if user.vote_weight != new_vote_weight:
      logging.info(
          'Vote weight changing from %d to %d for %s', user.vote_weight,
          new_vote_weight, user.nickname)

    new_roles = sorted(list(new_roles))
    user.roles = new_roles
    user.vote_weight = new_vote_weight
    user.put()

    bigquery.UserRow.DeferCreate(
        email=user.email,
        timestamp=datetime.datetime.utcnow(),
        action=constants.USER_ACTION.ROLE_CHANGE,
        roles=new_roles)

  @classmethod
  @ndb.transactional(xg=True)  # User and KeyValueCache
  def UpdateRoles(cls, email_addr, add=None, remove=None):
    user = User.GetOrInsert(email_addr)
    new_roles = set(user.roles).union(add or set()).difference(remove or set())
    cls.SetRoles(email_addr, new_roles)

  def _pre_put_hook(self):
    # Ensure that the email address was properly converted to lowercase.
    assert self.key.id().lower() == self.key.id()

    self.roles = sorted(list(set(self.roles)))

  def _GetAllPermissions(self):
    permissions = set()
    for role in self.roles:
      role_permissions = getattr(constants.PERMISSIONS, 'SET_%s' % role, ())
      permissions = permissions.union(role_permissions)
    return permissions

  @property
  def permissions(self):
    if not hasattr(self, '_permissions'):
      self._permissions = self._GetAllPermissions()
    return self._permissions

  @property
  def email(self):
    return self.key.string_id()

  @property
  def nickname(self):
    return user_map.EmailToUsername(self.key.string_id())

  @property
  def is_admin(self):
    has_admin_role = bool(set(self.roles) & constants.USER_ROLE.SET_ADMIN_ROLES)
    is_failsafe = self.email in settings.FAILSAFE_ADMINISTRATORS
    return has_admin_role or is_failsafe

  def HasRolloutGroup(self):
    """Indicates if this User has a rollout_group assigned."""
    return self.rollout_group != _UNASSIGNED_ROLLOUT_GROUP

  def AssignRolloutGroup(self):
    """Assigns a rollout_group value to this User.


    Returns:
      True if a rollout_group was assigned, False otherwise.
    """
    if not self.HasRolloutGroup():
      self.rollout_group = random.randrange(0, _ROLLOUT_GROUP_COUNT)
      return True
    return False

  def HasPermissionTo(self, task):
    """Verifies the User has permission to complete a task.

    Args:
      task: str, task being gated by permissions. One of constants.PERMISSIONS.*

    Returns:
      Boolean. True if user has the requested permission.
    """
    return self.is_admin or task in self.permissions


class AuditLog(BaseModelMixin, ndb.Model):
  """Represents an entry in the audit log.

  Records the creation or change of any model, except for events and rules,
  which are indelible.

  Attributes:
    log_event: str, the event being inserted into the log.
    user: string, the e-mail of the user associated with the event.
    target_object_key: ndb.Key, the key of the target object.
    recorded_dt: datetime, the insertion time.
  """
  log_event = ndb.StringProperty()
  user = ndb.StringProperty()
  target_object_key = ndb.KeyProperty(default=None)
  recorded_dt = ndb.DateTimeProperty(auto_now_add=True)

  @property
  def target_object_type(self):
    return self.target_object_key.kind()

  @property
  def target_object_id(self):
    return self.target_object_key.id()

  @classmethod
  def Create(cls, entity, message, user=None):
    """Adds an AuditLog to the given entity.

    Args:
      entity: The NDB entity that an AuditLog is being created for.
      message: The actual message string.
      user: Optional user email string.

    Returns:
      An ndb.Future that resolved to the key of the created AuditLog.
    """
    future = cls.CreateAsync(entity, message, user=user)
    return future.get_result()

  @classmethod
  def CreateAsync(cls, entity, message, user=None):
    """Asynchronously adds an AuditLog to the given entity."""
    audit_log = cls.New(entity, message, user=user)
    return audit_log.put_async()

  @classmethod
  def New(cls, entity, message, user=None):
    """Return a new AuditLog instance."""
    return cls(
        log_event=message,
        user=user,
        target_object_key=entity.key,
        parent=entity.key)

  @classmethod
  def GetAll(cls, entity, ascending=True):
    """Retrieves all AuditLogs for the given entity.

    Args:
      entity: The NDB entity whose AuditLogs we desire to have.
      ascending: Whether to order ascending or descending by creation timestamp.

    Returns:
      A list of AuditLogs.
    """
    query = cls.query(cls.target_object_key == entity.key)
    if ascending:
      query = query.order(cls.recorded_dt)
    else:
      query = query.order(-cls.recorded_dt)
    return query.fetch()

  def to_dict(self, include=None, exclude=None):  # pylint: disable=g-bad-name
    """Convert the model to a dict."""
    result = super(AuditLog, self).to_dict(include=include, exclude=exclude)

    if self.target_object_key is not None:
      result['target_object_id'] = self.target_object_id
      result['target_object_type'] = self.target_object_type

    return result


class Blacklist(ndb.Model):
  """Model for storing regular expressions for items that should be blacklisted.

  Attributes:
    regex: str, regular expression whose matches should be blacklisted.
    updated_dt: datetime, when the state was last changed.
  """
  regex = ndb.StringProperty()
  updated_dt = ndb.DateTimeProperty(auto_now=True)

  @classmethod
  def GetBlacklist(cls):
    """Returns all Blacklist entries."""
    entries = memcache.get(_BLACKLIST_MEMCACHE_KEY) or []
    if not entries:
      entries = Blacklist.query().fetch()
      memcache.set(
          _BLACKLIST_MEMCACHE_KEY, entries, time=_BLACKLIST_MEMCACHE_EXPIRATION)
    return entries

  @classmethod
  def IsBlacklisted(cls, text):
    """Returns True if the text matches a blacklist entry, False otherwise."""
    for entry in Blacklist.GetBlacklist():
      if re.match(entry.regex, text):
        return True
    return False
