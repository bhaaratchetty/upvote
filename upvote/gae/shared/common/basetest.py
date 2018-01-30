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

"""Base TestCase for Upvote AppEngine unit tests."""

import base64
import contextlib
import os
import pickle

import mock
from oauth2client.contrib import xsrfutil
import webapp2
from webob import exc
import webtest

from google.appengine.api import users
from google.appengine.datastore import datastore_stub_util

from common.testing import basetest

from upvote.gae.shared.common import handlers
from upvote.gae.shared.common import settings
from upvote.gae.shared.common import xsrf_utils
from upvote.gae.shared.models import test_utils
from upvote.shared import constants


class UpvoteTestCase(basetest.AppEngineTestCase):
  """Base TestCase for Upvote AppEngine unit tests."""

  def setUp(self, wsgi_app=None, patch_generate_token=True):
    super(UpvoteTestCase, self).setUp()

    # Require index.yaml be observed so tests will fail if indices are absent.
    index_yaml_dir = os.path.join(
        os.path.dirname('.'), 'upvote/gae')
    policy = datastore_stub_util.PseudoRandomHRConsistencyPolicy(probability=1)
    self.testbed.init_datastore_v3_stub(
        consistency_policy=policy, require_indexes=True,
        root_path=index_yaml_dir)
    self.testbed.init_memcache_stub()

    if wsgi_app is not None:
      # Workaround for lack of "runtime" variable in test env.
      adapter = lambda r, h: webapp2.Webapp2HandlerAdapter(h)
      wsgi_app.router.set_adapter(adapter)
      handlers.CreateErrorHandlersForApplications([wsgi_app])
      self.testapp = webtest.TestApp(wsgi_app)
    else:
      self.testapp = None

    self.secret_key = 'test-secret'
    xsrf_utils.SiteXsrfSecret.SetInstance(secret=self.secret_key.encode('hex'))

    if patch_generate_token:
      self.Patch(xsrfutil, 'generate_token', return_value='token')

    self.Patch(settings, 'ENABLE_BIGQUERY_STREAMING', return_value=True)

  @contextlib.contextmanager
  def LoggedInUser(self, user=None, email_addr=None, admin=False):

    # Start with a logout just in case there is already an active login.
    previous_user = users.get_current_user()
    self.Logout()

    # Create a new user if needed.
    if not user:
      user = test_utils.CreateUser(admin=admin, email=email_addr)

    # Log in as the newly-created user.
    self.Login(user.email)

    # Yield the entity out into the surrounded context.
    yield user

    # Once we're finished, log out.
    self.Logout()

    # If there was an existing login, restore it.
    if previous_user:
      self.Login(previous_user.email())

  def PatchSetting(self, setting, value):
    patcher = mock.patch.dict(settings.__dict__, values={setting: value})
    self.addCleanup(patcher.stop)
    return patcher.start()

  def VerifyIncrementCalls(self, mock_metric, *args):
    """Verifies the Increment() calls of a given mock metric.

    Args:
      mock_metric: The mock metric to verify.
      *args: The expected arguments of the Increment() calls.
    """
    expected_args = list(args)
    expected_call_count = len(expected_args)

    increment_calls = mock_metric.Increment.call_args_list
    actual_args = [c[0][0] for c in increment_calls]

    self.assertEqual(expected_call_count, mock_metric.Increment.call_count)
    self.assertEqual(expected_args, actual_args)

  def assertEntityCount(self, model_class, expected_count, ancestor=None):  # pylint: disable=g-bad-name
    actual_count = model_class.query(ancestor=ancestor).count(keys_only=True)
    self.assertEqual(expected_count, actual_count)

  def assertTaskCount(self, queue_name, expected_count):  # pylint: disable=g-bad-name
    self.assertEqual(expected_count, len(self.GetTasks(queue_name)))

  def assertRoutesDefined(self, *args):
    if self.testapp is None:
      self.fail('No test WSGIApplication defined')

    for url in args:
      request = self.testapp.app.request_class.blank(url)

      # Attempt to match the URL against a Route. The Webapp2 documentation
      # seems to indicate that a non-match could be indicated by either a return
      # value of None, or an HTTPNotFound, hence the weird implementation here.
      match = None
      try:
        match = self.testapp.app.router.default_matcher(request)
      except exc.HTTPNotFound:
        pass

      if match is None:
        self.fail('Route "%s" is not defined' % url)

  def Patch(self, target, attribute, **kwargs):
    patcher = mock.patch.object(target, attribute, **kwargs)
    self.addCleanup(patcher.stop)
    return patcher.start()

  def PatchValidateXSRFToken(self):
    self.Patch(xsrfutil, 'validate_token')

  def GetTasks(self, queue_name=constants.TASK_QUEUE.DEFAULT):
    """Returns the contents of a task queue (fixing the empty-queue case)."""
    # taskqueue_stub.GetTasks raises a KeyError if no task has been added so we
    # check for this case manually.
    try:
      return self.taskqueue_stub.GetTasks(queue_name)
    except KeyError:
      return []

  def DrainTaskQueue(self, queue_name, limit=None):
    """Runs all tasks in the given queue, even those created by prior tasks.

    This method is loosely based on AppEngineTestCase.RunDeferredTasks. It
    should only be preferred over that method in cases where a deferred task can
    create further deferred tasks, and that behavior needs to be verified.

    Args:
      queue_name: The name of the task queue.
      limit: The maximum number of tasks to run. If None, no limit is imposed.
    """
    keep_running = True
    tasks_run = 0

    while keep_running:

      tasks = self.GetTasks(queue_name)
      keep_running = bool(tasks)

      for task in tasks:

        self._RunDeferredTask(queue_name, task, True)
        tasks_run += 1

        # If there's a limit and it was just hit, bail.
        if limit and tasks_run >= limit:
          keep_running = False
          break

  def FlushTaskQueue(self, queue_name=constants.TASK_QUEUE.DEFAULT):
    self.taskqueue_stub.FlushQueue(queue_name)

  def UnpackTaskQueue(
      self, queue_name=constants.TASK_QUEUE.DEFAULT, flush=True):
    """Unpacks the contents of a specified task queue.

    Args:
      queue_name: The name of the task queue to unpack.
      flush: Whether or not to flush the contents of the task queue.

    Returns:
      A list of unpickled task queue payloads. Each item in the list is a
      triple of the form (function, list, dict). The function is the actual
      function object that was deferred to the task queue. The list contains all
      args passed to that function, and the dict contains all kwargs.
    """
    tasks = self.GetTasks(queue_name)
    if flush:
      self.FlushTaskQueue(queue_name=queue_name)
    # Unpack the task payloads.
    return [pickle.loads(base64.b64decode(task['body'])) for task in tasks]


def main():
  basetest.main()
