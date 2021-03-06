#!/usr/bin/env python
"""Tests for frontend server, client communicator, and the GRRHTTPClient."""

import array
import logging
import pdb
import time

import requests

from grr import config
from grr_response_client import comms
from grr_response_client.client_actions import admin
from grr_response_client.client_actions import standard
from grr.lib import communicator
from grr.lib import flags
from grr.lib import queues
from grr.lib import rdfvalue
from grr.lib import stats
from grr.lib import utils
from grr.lib.rdfvalues import client as rdf_client
from grr.lib.rdfvalues import crypto as rdf_crypto
from grr.lib.rdfvalues import flows as rdf_flows
from grr.lib.rdfvalues import protodict as rdf_protodict
from grr.server.grr_response_server import aff4
from grr.server.grr_response_server import data_store
from grr.server.grr_response_server import flow
from grr.server.grr_response_server import front_end
from grr.server.grr_response_server import maintenance_utils
from grr.server.grr_response_server import queue_manager
from grr.server.grr_response_server.aff4_objects import aff4_grr
from grr.server.grr_response_server.flows.general import ca_enroller
from grr.test_lib import client_test_lib
from grr.test_lib import flow_test_lib
from grr.test_lib import front_end_test_lib
from grr.test_lib import test_lib
from grr.test_lib import worker_mocks


class SendingTestFlow(flow.GRRFlow):
  """Tests that sent messages are correctly collected."""

  @flow.StateHandler()
  def Start(self):
    for i in range(10):
      self.CallClient(
          client_test_lib.Test,
          rdf_protodict.DataBlob(string="test%s" % i),
          data=str(i),
          next_state="Incoming")


class GRRFEServerTest(front_end_test_lib.FrontEndServerTest):
  """Tests the GRRFEServer."""

  def testReceiveMessages(self):
    """Test Receiving messages with no status."""
    client_id = test_lib.TEST_CLIENT_ID
    flow_obj = self.FlowSetup(
        flow_test_lib.FlowOrderTest.__name__, client_id=client_id)

    session_id = flow_obj.session_id
    messages = [
        rdf_flows.GrrMessage(
            request_id=1,
            response_id=i,
            session_id=session_id,
            payload=rdfvalue.RDFInteger(i)) for i in range(1, 10)
    ]

    self.server.ReceiveMessages(client_id, messages)

    # Make sure the task is still on the client queue
    manager = queue_manager.QueueManager(token=self.token)
    tasks_on_client_queue = manager.Query(client_id, 100)
    self.assertEqual(len(tasks_on_client_queue), 1)

    stored_messages = data_store.DB.ReadResponsesForRequestId(session_id, 1)

    self.assertEqual(len(stored_messages), len(messages))

    stored_messages.sort(key=lambda m: m.response_id)
    # Check that messages were stored correctly
    for stored_message, message in zip(stored_messages, messages):
      # We don't care about the last queueing time.
      stored_message.timestamp = None
      self.assertRDFValuesEqual(stored_message, message)

  def testReceiveMessagesWithStatus(self):
    """Receiving a sequence of messages with a status."""
    client_id = test_lib.TEST_CLIENT_ID
    flow_obj = self.FlowSetup(
        flow_test_lib.FlowOrderTest.__name__, client_id=client_id)

    session_id = flow_obj.session_id
    messages = [
        rdf_flows.GrrMessage(
            request_id=1,
            response_id=i,
            session_id=session_id,
            payload=rdfvalue.RDFInteger(i),
            task_id=15) for i in range(1, 10)
    ]

    # Now add the status message
    status = rdf_flows.GrrStatus(status=rdf_flows.GrrStatus.ReturnedStatus.OK)
    messages.append(
        rdf_flows.GrrMessage(
            request_id=1,
            response_id=len(messages) + 1,
            task_id=15,
            session_id=messages[0].session_id,
            payload=status,
            type=rdf_flows.GrrMessage.Type.STATUS))

    self.server.ReceiveMessages(client_id, messages)

    # Make sure the task is still on the client queue
    manager = queue_manager.QueueManager(token=self.token)
    tasks_on_client_queue = manager.Query(client_id, 100)
    self.assertEqual(len(tasks_on_client_queue), 1)

    stored_messages = data_store.DB.ReadResponsesForRequestId(session_id, 1)

    self.assertEqual(len(stored_messages), len(messages))

    stored_messages.sort(key=lambda m: m.response_id)
    # Check that messages were stored correctly
    for stored_message, message in zip(stored_messages, messages):
      # We don't care about the last queueing time.
      stored_message.timestamp = None
      self.assertRDFValuesEqual(stored_message, message)

  def testReceiveUnsolicitedClientMessage(self):
    client_id = test_lib.TEST_CLIENT_ID
    flow_obj = self.FlowSetup(
        flow_test_lib.FlowOrderTest.__name__, client_id=client_id)

    session_id = flow_obj.session_id
    status = rdf_flows.GrrStatus(status=rdf_flows.GrrStatus.ReturnedStatus.OK)
    messages = [
        # This message has no task_id set...
        rdf_flows.GrrMessage(
            request_id=1,
            response_id=1,
            session_id=session_id,
            payload=rdfvalue.RDFInteger(1),
            task_id=15),
        rdf_flows.GrrMessage(
            request_id=1,
            response_id=2,
            session_id=session_id,
            payload=status,
            type=rdf_flows.GrrMessage.Type.STATUS)
    ]

    self.server.ReceiveMessages(client_id, messages)
    manager = queue_manager.QueueManager(token=self.token)
    completed = list(manager.FetchCompletedRequests(session_id))
    self.assertEqual(len(completed), 1)

  def testWellKnownFlows(self):
    """Make sure that well known flows can run on the front end."""
    flow_test_lib.WellKnownSessionTest.messages = []
    session_id = flow_test_lib.WellKnownSessionTest.well_known_session_id

    messages = [
        rdf_flows.GrrMessage(
            request_id=0,
            response_id=0,
            session_id=session_id,
            payload=rdfvalue.RDFInteger(i)) for i in range(1, 10)
    ]

    self.server.ReceiveMessages(test_lib.TEST_CLIENT_ID, messages)

    # Wait for async actions to complete
    self.server.thread_pool.Join()

    flow_test_lib.WellKnownSessionTest.messages.sort()

    # Well known flows are now directly processed on the front end
    self.assertEqual(flow_test_lib.WellKnownSessionTest.messages,
                     list(range(1, 10)))

  def testWellKnownFlowsRemote(self):
    """Make sure that flows that do not exist on the front end get scheduled."""
    flow_test_lib.WellKnownSessionTest.messages = []
    session_id = flow_test_lib.WellKnownSessionTest.well_known_session_id

    messages = [
        rdf_flows.GrrMessage(
            request_id=0,
            response_id=0,
            session_id=session_id,
            payload=rdfvalue.RDFInteger(i)) for i in range(1, 10)
    ]

    # Delete the local well known flow cache is empty.
    self.server.well_known_flows = {}
    self.server.ReceiveMessages(test_lib.TEST_CLIENT_ID, messages)

    # Wait for async actions to complete
    self.server.thread_pool.Join()

    # None get processed now
    self.assertEqual(flow_test_lib.WellKnownSessionTest.messages, [])

    # The well known flow messages should be queued now.
    responses = data_store.DB.ReadResponsesForRequestId(session_id, 0)
    self.assertEqual(len(responses), 9)

  def testWellKnownFlowsNotifications(self):
    flow_test_lib.WellKnownSessionTest.messages = []
    flow_test_lib.WellKnownSessionTest2.messages = []
    session_id1 = flow_test_lib.WellKnownSessionTest.well_known_session_id
    session_id2 = flow_test_lib.WellKnownSessionTest2.well_known_session_id

    messages = []
    for i in range(1, 5):
      messages.append(
          rdf_flows.GrrMessage(
              request_id=0,
              response_id=0,
              session_id=session_id1,
              payload=rdfvalue.RDFInteger(i)))
      messages.append(
          rdf_flows.GrrMessage(
              request_id=0,
              response_id=0,
              session_id=session_id2,
              payload=rdfvalue.RDFInteger(i)))

    # This test whitelists only one flow.
    self.assertIn(session_id1.FlowName(), self.server.well_known_flows)
    self.assertNotIn(session_id2.FlowName(), self.server.well_known_flows)

    self.server.ReceiveMessages(test_lib.TEST_CLIENT_ID, messages)

    # Wait for async actions to complete
    self.server.thread_pool.Join()

    # Flow 1 should have been processed right away.
    flow_test_lib.WellKnownSessionTest.messages.sort()
    self.assertEqual(flow_test_lib.WellKnownSessionTest.messages,
                     list(range(1, 5)))

    # But not Flow 2.
    self.assertEqual(flow_test_lib.WellKnownSessionTest2.messages, [])

    manager = queue_manager.WellKnownQueueManager(token=self.token)

    notifications = manager.GetNotificationsForAllShards(session_id1.Queue())

    # Flow 1 was proecessed on the frontend, no queued responses available.
    responses = list(manager.FetchResponses(session_id1))
    self.assertEqual(responses, [])
    # And also no notifications.
    self.assertNotIn(
        session_id1,
        [notification.session_id for notification in notifications])

    # But for Flow 2 there should be some responses + a notification.
    responses = list(manager.FetchResponses(session_id2))
    self.assertEqual(len(responses), 4)
    self.assertIn(session_id2,
                  [notification.session_id for notification in notifications])

  def testDrainUpdateSessionRequestStates(self):
    """Draining the flow requests and preparing messages."""
    client_id = test_lib.TEST_CLIENT_ID
    # This flow sends 10 messages on Start()
    flow_obj = self.FlowSetup("SendingTestFlow", client_id=client_id)
    session_id = flow_obj.session_id

    # There should be 10 messages in the client's task queue
    manager = queue_manager.QueueManager(token=self.token)
    tasks = manager.Query(client_id, 100)
    self.assertEqual(len(tasks), 10)

    requests_by_id = {}

    for request, _ in data_store.DB.ReadRequestsAndResponses(session_id):
      requests_by_id[request.id] = request

    # Check that the response state objects have the correct ts_id set
    # in the client_queue:
    for task in tasks:
      request_id = task.request_id

      # Retrieve the request state for this request_id
      request = requests_by_id[request_id]

      # Check that task_id for the client message is correctly set in
      # request_state.
      self.assertEqual(request.request.task_id, task.task_id)

    # Now ask the server to drain the outbound messages into the
    # message list.
    response = rdf_flows.MessageList()

    response.job = self.server.DrainTaskSchedulerQueueForClient(client_id, 5)

    # Check that we received only as many messages as we asked for
    self.assertEqual(len(response.job), 5)

    for i in range(4):
      self.assertEqual(response.job[i].session_id, session_id)
      self.assertEqual(response.job[i].name, "Test")

  def testHandleMessageBundle(self):
    """Check that HandleMessageBundles() requeues messages if it failed.

    This test makes sure that when messages are pending for a client, and which
    we have no certificate for, the messages are requeued when sending fails.
    """
    # Make a new fake client
    client_id = self.SetupClient(0)

    class MockCommunicator(object):
      """A fake that simulates an unenrolled client."""

      def DecodeMessages(self, *unused_args):
        """For simplicity client sends an empty request."""
        return ([], client_id, 100)

      def EncodeMessages(self, *unused_args, **unused_kw):
        """Raise because the server has no certificates for this client."""
        raise communicator.UnknownClientCert()

    # Install the mock.
    self.server._communicator = MockCommunicator()

    # First request, the server will raise UnknownClientCert.
    request_comms = rdf_flows.ClientCommunication()
    self.assertRaises(communicator.UnknownClientCert,
                      self.server.HandleMessageBundles, request_comms, 2)

    # We can still schedule a flow for it
    flow.GRRFlow.StartFlow(
        client_id=client_id,
        flow_name=flow_test_lib.SendingFlow.__name__,
        message_count=1,
        token=self.token)
    manager = queue_manager.QueueManager(token=self.token)
    tasks = manager.Query(client_id, limit=100)

    self.assertRaises(communicator.UnknownClientCert,
                      self.server.HandleMessageBundles, request_comms, 2)

    new_tasks = manager.Query(client_id, limit=100)

    # The different in eta times reflect the lease that the server took on the
    # client messages.
    lease_time = (new_tasks[0].eta - tasks[0].eta) / 1e6

    # This lease time must be small, as the HandleMessageBundles() call failed,
    # the pending client messages must be put back on the queue.
    self.assertLess(lease_time, 1)

    # Since the server tried to send it, the ttl must be decremented
    self.assertEqual(tasks[0].task_ttl - new_tasks[0].task_ttl, 1)

  def _ScheduleResponseAndStatus(self, client_id, flow_id):
    with queue_manager.QueueManager(token=self.token) as flow_manager:
      # Schedule a response.
      flow_manager.QueueResponse(
          rdf_flows.GrrMessage(
              source=client_id,
              session_id=flow_id,
              payload=rdf_protodict.DataBlob(string="Helllo"),
              request_id=1,
              response_id=1))
      # And a STATUS message.
      flow_manager.QueueResponse(
          rdf_flows.GrrMessage(
              source=client_id,
              session_id=flow_id,
              payload=rdf_flows.GrrStatus(
                  status=rdf_flows.GrrStatus.ReturnedStatus.OK),
              request_id=1,
              response_id=2,
              type=rdf_flows.GrrMessage.Type.STATUS))

  def testHandleClientMessageRetransmission(self):
    """Check that requests get retransmitted but only if there is no status."""
    # Make a new fake client
    client_id = self.SetupClient(0)

    # Test the standard behavior.
    base_time = 1000
    msgs_recvd = []

    default_ttl = rdf_flows.GrrMessage().task_ttl
    with test_lib.FakeTime(base_time):
      flow.GRRFlow.StartFlow(
          client_id=client_id,
          flow_name=flow_test_lib.SendingFlow.__name__,
          message_count=1,
          token=self.token)

    for i in range(default_ttl):
      with test_lib.FakeTime(base_time + i * (self.MESSAGE_EXPIRY_TIME + 1)):

        tasks = self.server.DrainTaskSchedulerQueueForClient(client_id, 100000)
        msgs_recvd.append(tasks)

    # Should return a client message (ttl-1) times and nothing afterwards.
    self.assertEqual(
        map(bool, msgs_recvd),
        [True] * (rdf_flows.GrrMessage().task_ttl - 1) + [False])

    # Now we simulate that the workers are overloaded - the client messages
    # arrive but do not get processed in time.
    if default_ttl <= 3:
      self.fail("TTL too low for this test.")

    msgs_recvd = []

    with test_lib.FakeTime(base_time):
      flow_id = flow.GRRFlow.StartFlow(
          client_id=client_id,
          flow_name=flow_test_lib.SendingFlow.__name__,
          message_count=1,
          token=self.token)

    for i in range(default_ttl):
      if i == 2:
        self._ScheduleResponseAndStatus(client_id, flow_id)

      with test_lib.FakeTime(base_time + i * (self.MESSAGE_EXPIRY_TIME + 1)):

        tasks = self.server.DrainTaskSchedulerQueueForClient(client_id, 100000)
        msgs_recvd.append(tasks)

        if not tasks:
          # Even if the request has not been leased ttl times yet,
          # it should be dequeued by now.
          new_tasks = queue_manager.QueueManager(token=self.token).Query(
              queue=rdf_client.ClientURN(client_id).Queue(), limit=1000)
          self.assertEqual(len(new_tasks), 0)

    # Should return a client message twice and nothing afterwards.
    self.assertEqual(
        map(bool, msgs_recvd),
        [True] * 2 + [False] * (rdf_flows.GrrMessage().task_ttl - 2))


def MakeHTTPException(code=500, msg="Error"):
  """A helper for creating a HTTPError exception."""
  response = requests.Response()
  response.status_code = code
  return requests.ConnectionError(msg, response=response)


def MakeResponse(code=500, data=""):
  """A helper for creating a HTTPError exception."""
  response = requests.Response()
  response.status_code = code
  response._content = data
  return response


class ClientCommsTest(test_lib.GRRBaseTest):
  """Test the communicator."""

  def setUp(self):
    """Set up communicator tests."""
    super(ClientCommsTest, self).setUp()

    # These tests change the config so we preserve state.
    self.config_stubber = test_lib.PreserveConfig()
    self.config_stubber.Start()

    self.client_private_key = config.CONFIG["Client.private_key"]

    self.server_serial_number = 0
    self.server_certificate = config.CONFIG["Frontend.certificate"]
    self.server_private_key = config.CONFIG["PrivateKeys.server_key"]
    self.client_communicator = comms.ClientCommunicator(
        private_key=self.client_private_key)

    self.client_communicator.LoadServerCertificate(
        server_certificate=self.server_certificate,
        ca_certificate=config.CONFIG["CA.certificate"])

    self.last_urlmock_error = None

    self._SetupCommunicator()

  def _SetupCommunicator(self):
    self.server_communicator = front_end.ServerCommunicator(
        certificate=self.server_certificate,
        private_key=self.server_private_key,
        token=self.token)

  def tearDown(self):
    super(ClientCommsTest, self).tearDown()
    self.config_stubber.Stop()

  def _LabelClient(self, client_id, label):
    with aff4.FACTORY.Open(
        client_id, mode="rw", token=self.token) as client_object:
      client_object.AddLabel(label)

  def ClientServerCommunicate(self, timestamp=None):
    """Tests the end to end encrypted communicators."""
    message_list = rdf_flows.MessageList()
    for i in range(1, 11):
      message_list.job.Append(
          session_id=rdfvalue.SessionID(
              base="aff4:/flows", queue=queues.FLOWS, flow_name=i),
          name="OMG it's a string")

    result = rdf_flows.ClientCommunication()
    timestamp = self.client_communicator.EncodeMessages(
        message_list, result, timestamp=timestamp)
    self.cipher_text = result.SerializeToString()

    (decoded_messages, source, client_timestamp) = (
        self.server_communicator.DecryptMessage(self.cipher_text))

    self.assertEqual(source, self.client_communicator.common_name)
    self.assertEqual(client_timestamp, timestamp)
    self.assertEqual(len(decoded_messages), 10)
    for i in range(1, 11):
      self.assertEqual(
          decoded_messages[i - 1].session_id,
          rdfvalue.SessionID(
              base="aff4:/flows", queue=queues.FLOWS, flow_name=i))

    return decoded_messages

  def testCommunications(self):
    """Test that messages from unknown clients are tagged unauthenticated."""
    decoded_messages = self.ClientServerCommunicate()
    for i in range(len(decoded_messages)):
      self.assertEqual(decoded_messages[i].auth_state,
                       rdf_flows.GrrMessage.AuthorizationState.UNAUTHENTICATED)

  def _MakeClientRecord(self):
    """Make a client in the data store."""
    client_cert = self.ClientCertFromPrivateKey(self.client_private_key)
    self.client_id = client_cert.GetCN()
    new_client = aff4.FACTORY.Create(
        self.client_id, aff4_grr.VFSGRRClient, token=self.token)
    new_client.Set(new_client.Schema.CERT, client_cert)
    new_client.Close()
    return new_client

  def testKnownClient(self):
    """Test that messages from known clients are authenticated."""
    self._MakeClientRecord()

    # Now the server should know about it
    decoded_messages = self.ClientServerCommunicate()

    for i in range(len(decoded_messages)):
      self.assertEqual(decoded_messages[i].auth_state,
                       rdf_flows.GrrMessage.AuthorizationState.AUTHENTICATED)

  def testClientPingAndClockIsUpdated(self):
    """Check PING and CLOCK are updated, simulate bad client clock."""
    self._MakeClientRecord()
    now = rdfvalue.RDFDatetime.Now()
    client_now = now - 20
    with test_lib.FakeTime(now):
      self.ClientServerCommunicate(timestamp=client_now)

      client_obj = aff4.FACTORY.Open(self.client_id, token=self.token)
      self.assertEqual(now, client_obj.Get(client_obj.Schema.PING))
      self.assertEqual(client_now, client_obj.Get(client_obj.Schema.CLOCK))

    now += 60
    client_now += 40
    with test_lib.FakeTime(now):
      self.ClientServerCommunicate(timestamp=client_now)

      client_obj = aff4.FACTORY.Open(self.client_id, token=self.token)
      self.assertEqual(now, client_obj.Get(client_obj.Schema.PING))
      self.assertEqual(client_now, client_obj.Get(client_obj.Schema.CLOCK))

  def testClientPingStatsUpdated(self):
    """Check client ping stats are updated."""
    self._MakeClientRecord()
    current_pings = stats.STATS.GetMetricValue(
        "client_pings_by_label", fields=["testlabel"])

    self._LabelClient(self.client_id, "testlabel")

    now = rdfvalue.RDFDatetime.Now()
    with test_lib.FakeTime(now):
      self.ClientServerCommunicate(timestamp=now)

    new_pings = stats.STATS.GetMetricValue(
        "client_pings_by_label", fields=["testlabel"])
    self.assertEqual(new_pings, current_pings + 1)

  def testServerReplayAttack(self):
    """Test that replaying encrypted messages to the server invalidates them."""
    self._MakeClientRecord()

    # First send some messages to the server
    decoded_messages = self.ClientServerCommunicate(timestamp=1000000)

    encrypted_messages = self.cipher_text

    self.assertEqual(decoded_messages[0].auth_state,
                     rdf_flows.GrrMessage.AuthorizationState.AUTHENTICATED)

    # Immediate replay is accepted by the server since some proxies do this.
    (decoded_messages, _,
     _) = self.server_communicator.DecryptMessage(encrypted_messages)

    self.assertEqual(decoded_messages[0].auth_state,
                     rdf_flows.GrrMessage.AuthorizationState.AUTHENTICATED)

    # Move the client time more than 1h forward.
    self.ClientServerCommunicate(timestamp=1000000 + 3700 * 1000000)

    # And replay the old messages again.
    (decoded_messages, _,
     _) = self.server_communicator.DecryptMessage(encrypted_messages)

    # Messages should now be tagged as desynced
    self.assertEqual(decoded_messages[0].auth_state,
                     rdf_flows.GrrMessage.AuthorizationState.DESYNCHRONIZED)

  def testX509Verify(self):
    """X509 Verify can have several failure paths."""

    # This is a successful verify.
    with utils.Stubber(
        rdf_crypto.RDFX509Cert, "Verify", lambda self, public_key=None: True):
      self.client_communicator.LoadServerCertificate(
          self.server_certificate, config.CONFIG["CA.certificate"])

    def Verify(_, public_key=False):
      _ = public_key
      raise rdf_crypto.VerificationError("Testing verification failure.")

    # Mock the verify function to simulate certificate failures.
    with utils.Stubber(rdf_crypto.RDFX509Cert, "Verify", Verify):
      self.assertRaises(IOError, self.client_communicator.LoadServerCertificate,
                        self.server_certificate,
                        config.CONFIG["CA.certificate"])

  def testErrorDetection(self):
    """Tests the end to end encrypted communicators."""
    # Install the client - now we can verify its signed messages
    self._MakeClientRecord()

    # Make something to send
    message_list = rdf_flows.MessageList()
    for i in range(0, 10):
      message_list.job.Append(session_id=str(i))

    result = rdf_flows.ClientCommunication()
    self.client_communicator.EncodeMessages(message_list, result)
    cipher_text = result.SerializeToString()

    # Depending on this modification several things may happen:
    # 1) The padding may not match which will cause a decryption exception.
    # 2) The protobuf may fail to decode causing a decoding exception.
    # 3) The modification may affect the signature resulting in UNAUTHENTICATED
    #    messages.
    # 4) The modification may have no effect on the data at all.
    for x in range(0, len(cipher_text), 50):
      # Futz with the cipher text (Make sure it's really changed)
      mod_cipher_text = (
          cipher_text[:x] + chr((ord(cipher_text[x]) % 250) + 1) +
          cipher_text[x + 1:])

      try:
        decoded, client_id, _ = self.server_communicator.DecryptMessage(
            mod_cipher_text)

        for i, message in enumerate(decoded):
          # If the message is actually authenticated it must not be changed!
          if message.auth_state == message.AuthorizationState.AUTHENTICATED:
            self.assertEqual(message.source, client_id)

            # These fields are set by the decoder and are not present in the
            # original message - so we clear them before comparison.
            message.auth_state = None
            message.source = None
            self.assertRDFValuesEqual(message, message_list.job[i])
          else:
            logging.debug("Message %s: Authstate: %s", i, message.auth_state)

      except communicator.DecodingError as e:
        logging.debug("Detected alteration at %s: %s", x, e)

  def testEnrollingCommunicator(self):
    """Test that the ClientCommunicator generates good keys."""
    self.client_communicator = comms.ClientCommunicator()

    self.client_communicator.LoadServerCertificate(
        self.server_certificate, config.CONFIG["CA.certificate"])

    # Verify that the CN is of the correct form
    csr = self.client_communicator.GetCSR()
    cn = rdf_client.ClientURN.FromPublicKey(csr.GetPublicKey())
    self.assertEqual(cn, csr.GetCN())

  def testServerKeyRotation(self):
    self._MakeClientRecord()

    # Now the server should know about the client.
    decoded_messages = self.ClientServerCommunicate()
    for i in range(len(decoded_messages)):
      self.assertEqual(decoded_messages[i].auth_state,
                       rdf_flows.GrrMessage.AuthorizationState.AUTHENTICATED)

    # Suppress the output.
    with utils.Stubber(maintenance_utils, "EPrint", lambda msg: None):
      maintenance_utils.RotateServerKey()

    server_certificate = config.CONFIG["Frontend.certificate"]
    server_private_key = config.CONFIG["PrivateKeys.server_key"]

    self.assertNotEqual(server_certificate, self.server_certificate)
    self.assertNotEqual(server_private_key, self.server_private_key)

    self.server_communicator = front_end.ServerCommunicator(
        certificate=server_certificate,
        private_key=server_private_key,
        token=self.token)

    # Clients can't connect at this point since they use the outdated
    # session key.
    with self.assertRaises(communicator.DecryptionError):
      self.ClientServerCommunicate()

    # After the client reloads the server cert, this should start
    # working again.
    self.client_communicator.LoadServerCertificate(
        server_certificate=server_certificate,
        ca_certificate=config.CONFIG["CA.certificate"])
    self.assertEqual(len(list(self.ClientServerCommunicate())), 10)


class HTTPClientTests(test_lib.GRRBaseTest):
  """Test the http communicator."""

  def setUp(self):
    """Set up communicator tests."""
    super(HTTPClientTests, self).setUp()

    # These tests change the config so we preserve state.
    self.config_stubber = test_lib.PreserveConfig()
    self.config_stubber.Start()

    self.server_serial_number = 0

    self.server_private_key = config.CONFIG["PrivateKeys.server_key"]
    self.server_certificate = config.CONFIG["Frontend.certificate"]

    # Make a new client
    self.CreateNewClientObject()

    # The housekeeper threads of the time based caches also call time.time and
    # interfere with some tests so we disable them here.
    utils.InterruptableThread.exit = True
    # The same also applies to the StatsCollector thread.
    stats.StatsCollector.exit = True

    # And cache it in the server
    self.CreateNewServerCommunicator()

    self.requests_stubber = utils.Stubber(requests, "request", self.UrlMock)
    self.requests_stubber.Start()
    self.sleep_stubber = utils.Stubber(time, "sleep", lambda x: None)
    self.sleep_stubber.Start()

    self.messages = []

    ca_enroller.enrolment_cache.Flush()

    # Response to send back to clients.
    self.server_response = dict(
        session_id="aff4:/W:session", name="Echo", response_id=2)

  def _MakeClient(self):
    self.client_certificate = self.ClientCertFromPrivateKey(
        config.CONFIG["Client.private_key"])
    self.client_cn = self.client_certificate.GetCN()

    self.client = aff4.FACTORY.Create(
        self.client_cn, aff4_grr.VFSGRRClient, mode="rw", token=self.token)
    self.client.Set(self.client.Schema.CERT(self.client_certificate.AsPEM()))
    self.client.Flush()

  def _ClearClient(self):
    self.server_communicator.client_cache.Flush()

    # Assume we do not know the client yet by clearing its certificate.
    self.client = aff4.FACTORY.Create(
        self.client_cn, aff4_grr.VFSGRRClient, mode="rw", token=self.token)
    self.client.DeleteAttribute(self.client.Schema.CERT)
    self.client.Flush()

  def CreateNewServerCommunicator(self):
    self._MakeClient()
    self.server_communicator = front_end.ServerCommunicator(
        certificate=self.server_certificate,
        private_key=self.server_private_key,
        token=self.token)

    self.server_communicator.client_cache.Put(self.client_cn, self.client)

  def tearDown(self):
    self.requests_stubber.Stop()
    self.config_stubber.Stop()
    self.sleep_stubber.Stop()
    super(HTTPClientTests, self).tearDown()

  def CreateClientCommunicator(self):
    self.client_communicator = comms.GRRHTTPClient(
        ca_cert=config.CONFIG["CA.certificate"],
        worker_cls=worker_mocks.DisabledNannyClientWorker)

  def CreateNewClientObject(self):
    self.CreateClientCommunicator()

    # Disable stats collection for tests.
    self.client_communicator.client_worker.last_stats_sent_time = (
        time.time() + 3600)

    # Build a client context with preloaded server certificates
    self.client_communicator.communicator.LoadServerCertificate(
        self.server_certificate, config.CONFIG["CA.certificate"])

    self.client_communicator.http_manager.retry_error_limit = 5

  def UrlMock(self, num_messages=10, url=None, data=None, **kwargs):
    """A mock for url handler processing from the server's POV."""
    if "server.pem" in url:
      return MakeResponse(200,
                          utils.SmartStr(config.CONFIG["Frontend.certificate"]))

    _ = kwargs
    try:
      comms_cls = rdf_flows.ClientCommunication
      self.client_communication = comms_cls.FromSerializedString(data)

      # Decrypt incoming messages
      self.messages, source, ts = self.server_communicator.DecodeMessages(
          self.client_communication)

      # Make sure the messages are correct
      self.assertEqual(source, self.client_cn)
      for i, message in enumerate(self.messages):
        # Do not check any status messages.
        if message.request_id:
          self.assertEqual(message.response_id, i)
          self.assertEqual(message.request_id, 1)
          self.assertEqual(message.session_id, "aff4:/W:session")

      # Now prepare a response
      response_comms = rdf_flows.ClientCommunication()
      message_list = rdf_flows.MessageList()
      for i in range(0, num_messages):
        message_list.job.Append(request_id=i, **self.server_response)

      # Preserve the timestamp as a nonce
      self.server_communicator.EncodeMessages(
          message_list,
          response_comms,
          destination=source,
          timestamp=ts,
          api_version=self.client_communication.api_version)

      return MakeResponse(200, response_comms.SerializeToString())
    except communicator.UnknownClientCert:
      raise MakeHTTPException(406)
    except Exception as e:
      logging.info("Exception in mock urllib2.Open: %s.", e)
      self.last_urlmock_error = e

      if flags.FLAGS.debug:
        pdb.post_mortem()

      raise MakeHTTPException(500)

  def CheckClientQueue(self):
    """Checks that the client context received all server messages."""
    # Check the incoming messages
    self.assertEqual(self.client_communicator.client_worker.InQueueSize(), 10)

    for i, message in enumerate(
        self.client_communicator.client_worker._in_queue.queue):
      # This is the common name embedded in the certificate.
      self.assertEqual(message.source, "aff4:/GRR Test Server")
      self.assertEqual(message.response_id, 2)
      self.assertEqual(message.request_id, i)
      self.assertEqual(message.session_id, "aff4:/W:session")
      self.assertEqual(message.auth_state,
                       rdf_flows.GrrMessage.AuthorizationState.AUTHENTICATED)

    # Clear the queue
    self.client_communicator.client_worker._in_queue.queue.clear()

  def SendToServer(self):
    """Schedule some packets from client to server."""
    # Generate some client traffic
    for i in range(0, 10):
      self.client_communicator.client_worker.SendReply(
          rdf_flows.GrrStatus(),
          session_id=rdfvalue.SessionID("W:session"),
          response_id=i,
          request_id=1)

  def testInitialEnrollment(self):
    """If the client has no certificate initially it should enroll."""

    # Clear the certificate so we can generate a new one.
    with test_lib.ConfigOverrider({
        "Client.private_key": "",
    }):
      self.CreateNewClientObject()

      # Client should get a new Common Name.
      self.assertNotEqual(self.client_cn,
                          self.client_communicator.communicator.common_name)

      self.client_cn = self.client_communicator.communicator.common_name

      # The client will sleep and re-attempt to connect multiple times.
      status = self.client_communicator.RunOnce()

      self.assertEqual(status.code, 406)

      # The client should now send an enrollment request.
      status = self.client_communicator.RunOnce()

      # Client should generate enrollment message by itself.
      self.assertEqual(len(self.messages), 1)
      self.assertEqual(self.messages[0].session_id,
                       ca_enroller.Enroler.well_known_session_id)

  def testEnrollment(self):
    """Test the http response to unknown clients."""

    self._ClearClient()

    # Now communicate with the server.
    self.SendToServer()
    status = self.client_communicator.RunOnce()

    # We expect to receive a 406 and all client messages will be tagged as
    # UNAUTHENTICATED.
    self.assertEqual(status.code, 406)
    self.assertEqual(len(self.messages), 10)
    self.assertEqual(self.messages[0].auth_state,
                     rdf_flows.GrrMessage.AuthorizationState.UNAUTHENTICATED)

    # The next request should be an enrolling request.
    status = self.client_communicator.RunOnce()

    self.assertEqual(len(self.messages), 11)
    self.assertEqual(self.messages[-1].session_id,
                     ca_enroller.Enroler.well_known_session_id)

    # Now we manually run the enroll well known flow with the enrollment
    # request. This will start a new flow for enrolling the client, sign the
    # cert and add it to the data store.
    flow_obj = ca_enroller.Enroler(
        ca_enroller.Enroler.well_known_session_id, mode="rw", token=self.token)
    flow_obj.ProcessMessage(self.messages[-1])

    # The next client communication should be enrolled now.
    status = self.client_communicator.RunOnce()

    self.assertEqual(status.code, 200)

    # There should be a cert for the client right now.
    self.client = aff4.FACTORY.Create(
        self.client_cn, aff4_grr.VFSGRRClient, mode="rw", token=self.token)
    self.assertTrue(self.client.Get(self.client.Schema.CERT))

    # Now communicate with the server once again.
    self.SendToServer()
    status = self.client_communicator.RunOnce()

    self.assertEqual(status.code, 200)

  def testReboots(self):
    """Test the http communication with reboots."""
    # Now we add the new client record to the server cache
    self.SendToServer()
    self.client_communicator.RunOnce()
    self.CheckClientQueue()

    # Simulate the client rebooted
    self.CreateNewClientObject()

    self.SendToServer()
    self.client_communicator.RunOnce()
    self.CheckClientQueue()

    # Simulate the server rebooting
    self.CreateNewServerCommunicator()

    self.SendToServer()
    self.client_communicator.RunOnce()
    self.CheckClientQueue()

  def _CheckFastPoll(self, require_fastpoll, expected_sleeptime):
    self.server_response = dict(
        session_id="aff4:/W:session",
        name="Echo",
        response_id=2,
        priority="LOW_PRIORITY",
        require_fastpoll=require_fastpoll)

    # Make sure we don't have any output messages that might override the
    # fastpoll setting from the input messages we send
    self.assertEqual(self.client_communicator.client_worker.OutQueueSize(), 0)

    self.client_communicator.RunOnce()
    # Make sure the timer is set to the correct value.
    self.assertEqual(self.client_communicator.timer.sleep_time,
                     expected_sleeptime)
    self.CheckClientQueue()

  def testNoFastPoll(self):
    """Test that the fast poll False is respected on input messages.

    Also make sure we wait the correct amount of time before next poll.
    """
    self._CheckFastPoll(False, config.CONFIG["Client.poll_max"])

  def testFastPoll(self):
    """Test that the fast poll True is respected on input messages.

    Also make sure we wait the correct amount of time before next poll.
    """
    self._CheckFastPoll(True, config.CONFIG["Client.poll_min"])

  def testCorruption(self):
    """Simulate corruption of the http payload."""

    self.corruptor_field = None

    def Corruptor(url="", data=None, **kwargs):
      """Futz with some of the fields."""
      comm_cls = rdf_flows.ClientCommunication
      if data is not None:
        self.client_communication = comm_cls.FromSerializedString(data)
      else:
        self.client_communication = comm_cls(None)

      if self.corruptor_field and "server.pem" not in url:
        orig_str_repr = self.client_communication.SerializeToString()
        field_data = getattr(self.client_communication, self.corruptor_field)
        if hasattr(field_data, "SerializeToString"):
          # This converts encryption keys to a string so we can corrupt them.
          field_data = field_data.SerializeToString()

        modified_data = array.array("c", field_data)
        offset = len(field_data) / 2
        modified_data[offset] = chr((ord(field_data[offset]) % 250) + 1)
        setattr(self.client_communication, self.corruptor_field,
                modified_data.tostring())

        # Make sure we actually changed the data.
        self.assertNotEqual(field_data, modified_data)

        mod_str_repr = self.client_communication.SerializeToString()
        self.assertEqual(len(orig_str_repr), len(mod_str_repr))
        differences = [
            True for x, y in zip(orig_str_repr, mod_str_repr) if x != y
        ]
        self.assertEqual(len(differences), 1)

      data = self.client_communication.SerializeToString()
      return self.UrlMock(url=url, data=data, **kwargs)

    with utils.Stubber(requests, "request", Corruptor):
      self.SendToServer()
      status = self.client_communicator.RunOnce()
      self.assertEqual(status.code, 200)

      for field in ["packet_iv", "encrypted"]:
        # Corrupting each field should result in HMAC verification errors.
        self.corruptor_field = field

        self.SendToServer()
        status = self.client_communicator.RunOnce()

        self.assertEqual(status.code, 500)
        self.assertTrue(
            "HMAC verification failed" in str(self.last_urlmock_error))

      # Corruption of these fields will likely result in RSA errors, since we do
      # the RSA operations before the HMAC verification (in order to recover the
      # hmac key):
      for field in ["encrypted_cipher", "encrypted_cipher_metadata"]:
        # Corrupting each field should result in HMAC verification errors.
        self.corruptor_field = field

        self.SendToServer()
        status = self.client_communicator.RunOnce()

        self.assertEqual(status.code, 500)

  def testClientRetransmission(self):
    """Test that client retransmits failed messages."""
    fail = True
    num_messages = 10

    def FlakyServer(url=None, **kwargs):
      if not fail or "server.pem" in url:
        return self.UrlMock(num_messages=num_messages, url=url, **kwargs)

      raise MakeHTTPException(500)

    with utils.Stubber(requests, "request", FlakyServer):
      self.SendToServer()
      status = self.client_communicator.RunOnce()
      self.assertEqual(status.code, 500)

      # Server should not receive anything.
      self.assertEqual(len(self.messages), 0)

      # Try to send these messages again.
      fail = False

      self.assertEqual(self.client_communicator.client_worker.InQueueSize(), 0)

      status = self.client_communicator.RunOnce()

      self.assertEqual(status.code, 200)

      # We have received 10 client messages.
      self.assertEqual(self.client_communicator.client_worker.InQueueSize(), 10)
      self.CheckClientQueue()

      # Server should have received 10 messages this time.
      self.assertEqual(len(self.messages), 10)

  # TODO(hanuszczak): We have a separate test suite for the stat collector.
  # Most of these test methods are no longer required, especially that now they
  # need to use implementation-specific methods instead of the public API.

  def testClientStatsCollection(self):
    """Tests that the client stats are collected automatically."""
    now = 1000000
    # Pretend we have already sent stats.
    self.client_communicator.client_worker.stats_collector._last_send_time = (
        rdfvalue.RDFDatetime.FromSecondsSinceEpoch(now))

    with test_lib.FakeTime(now):
      self.client_communicator.client_worker.stats_collector._Send()

    runs = []
    with utils.Stubber(admin.GetClientStatsAuto, "Run",
                       lambda cls, _: runs.append(1)):

      # No stats collection after 10 minutes.
      with test_lib.FakeTime(now + 600):
        self.client_communicator.client_worker.stats_collector._Send()
        self.assertEqual(len(runs), 0)

      # Let one hour pass.
      with test_lib.FakeTime(now + 3600):
        self.client_communicator.client_worker.stats_collector._Send()
        # This time the client should collect stats.
        self.assertEqual(len(runs), 1)

      # Let one hour and ten minutes pass.
      with test_lib.FakeTime(now + 3600 + 600):
        self.client_communicator.client_worker.stats_collector._Send()
        # Again, there should be no stats collection, as last collection
        # happened less than an hour ago.
        self.assertEqual(len(runs), 1)

  def testClientStatsCollectionHappensEveryMinuteWhenClientIsBusy(self):
    """Tests that client stats are collected more often when client is busy."""
    now = 1000000
    # Pretend we have already sent stats.
    self.client_communicator.client_worker.stats_collector._last_send_time = (
        rdfvalue.RDFDatetime.FromSecondsSinceEpoch(now))
    self.client_communicator.client_worker._is_active = True

    with test_lib.FakeTime(now):
      self.client_communicator.client_worker.stats_collector._Send()

    runs = []
    with utils.Stubber(admin.GetClientStatsAuto, "Run",
                       lambda cls, _: runs.append(1)):

      # No stats collection after 30 seconds.
      with test_lib.FakeTime(now + 30):
        self.client_communicator.client_worker.stats_collector._Send()
        self.assertEqual(len(runs), 0)

      # Let 61 seconds pass.
      with test_lib.FakeTime(now + 61):
        self.client_communicator.client_worker.stats_collector._Send()
        # This time the client should collect stats.
        self.assertEqual(len(runs), 1)

      # No stats collection within one minute from the last time.
      with test_lib.FakeTime(now + 61 + 59):
        self.client_communicator.client_worker.stats_collector._Send()
        self.assertEqual(len(runs), 1)

      # Stats collection happens as more than one minute has passed since the
      # last one.
      with test_lib.FakeTime(now + 61 + 61):
        self.client_communicator.client_worker.stats_collector._Send()
        self.assertEqual(len(runs), 2)

  def testClientStatsCollectionAlwaysHappensAfterHandleMessage(self):
    """Tests that client stats are collected more often when client is busy."""
    now = 1000000
    # Pretend we have already sent stats.
    self.client_communicator.client_worker.stats_collector._last_send_time = (
        rdfvalue.RDFDatetime.FromSecondsSinceEpoch(now))

    with test_lib.FakeTime(now):
      self.client_communicator.client_worker.stats_collector._Send()

    runs = []
    with utils.Stubber(admin.GetClientStatsAuto, "Run",
                       lambda cls, _: runs.append(1)):

      # No stats collection after 30 seconds.
      with test_lib.FakeTime(now + 30):
        self.client_communicator.client_worker.stats_collector._Send()
        self.assertEqual(len(runs), 0)

      msg = rdf_flows.GrrMessage(
          name=standard.HashFile.__name__, generate_task_id=True)
      self.client_communicator.client_worker.HandleMessage(msg)

      # HandleMessage was called, but one minute hasn't passed, so
      # stats should not be sent.
      with test_lib.FakeTime(now + 59):
        self.client_communicator.client_worker.stats_collector._Send()
        self.assertEqual(len(runs), 0)

      # HandleMessage was called more than one minute ago, so stats
      # should be sent.
      with test_lib.FakeTime(now + 61):
        self.client_communicator.client_worker.stats_collector._Send()
        self.assertEqual(len(runs), 1)

  def RaiseError(self, **_):
    raise MakeHTTPException(500, "Not a real connection.")

  def testClientConnectionErrors(self):
    client_obj = comms.GRRHTTPClient(
        worker_cls=worker_mocks.DisabledNannyClientWorker)
    # Make the connection unavailable and skip the retry interval.
    with utils.MultiStubber(
        (requests, "request", self.RaiseError),
        (client_obj.http_manager, "connection_error_limit", 8)):
      # Simulate a client run. The client will retry the connection limit by
      # itself. The Run() method will quit when connection_error_limit is
      # reached. This will make the real client quit.
      client_obj.Run()

      self.assertEqual(client_obj.http_manager.consecutive_connection_errors, 9)


class RelationalClientCommsTest(ClientCommsTest):

  def _MakeClientRecord(self):
    """Make a client in the data store."""
    client_cert = self.ClientCertFromPrivateKey(self.client_private_key)
    self.client_id = client_cert.GetCN()[len("aff4:/"):]
    data_store.REL_DB.WriteClientMetadata(
        self.client_id, fleetspeak_enabled=False, certificate=client_cert)

  def _SetupCommunicator(self):
    self.server_communicator = front_end.RelationalServerCommunicator(
        certificate=self.server_certificate,
        private_key=self.server_private_key)

  def _LabelClient(self, client_id, label):
    data_store.REL_DB.AddClientLabels(client_id, "Test", [label])

  def testClientPingAndClockIsUpdated(self):
    """Check PING and CLOCK are updated."""

    self._MakeClientRecord()

    now = rdfvalue.RDFDatetime.Now()
    client_now = now - 20
    with test_lib.FakeTime(now):
      self.ClientServerCommunicate(timestamp=client_now)

    metadata = data_store.REL_DB.ReadClientMetadata(self.client_id)

    self.assertEqual(now, metadata.ping)
    self.assertEqual(client_now, metadata.clock)

    now += 60
    client_now += 40
    with test_lib.FakeTime(now):
      self.ClientServerCommunicate(timestamp=client_now)

    metadata = data_store.REL_DB.ReadClientMetadata(self.client_id)
    self.assertEqual(now, metadata.ping)
    self.assertEqual(client_now, metadata.clock)


class RelationalHTTPClientTests(HTTPClientTests):

  def _MakeClient(self):
    self.client_certificate = self.ClientCertFromPrivateKey(
        config.CONFIG["Client.private_key"])
    self.client_cn = self.client_certificate.GetCN()
    self.client_id = self.client_cn[len("aff4:/"):]

    data_store.REL_DB.WriteClientMetadata(
        self.client_id,
        certificate=self.client_certificate,
        fleetspeak_enabled=False)

  def CreateNewServerCommunicator(self):
    self._MakeClient()
    self.server_communicator = front_end.RelationalServerCommunicator(
        certificate=self.server_certificate,
        private_key=self.server_private_key)

  def _ClearClient(self):
    del data_store.REL_DB.delegate.metadatas[self.client_id]


def main(args):
  test_lib.main(args)


if __name__ == "__main__":
  flags.StartMain(main)
