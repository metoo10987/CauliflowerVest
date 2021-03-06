#!/usr/bin/env python
#
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
#
"""CauliflowerVest glue code module."""

import logging
import os
import plistlib


from cauliflowervest.client import util
from cauliflowervest.client.mac import corestorage


ENCRYPTION_SUCCESS_MESSAGE = (
    'Encryption enabled and passphrase escrowed successfully.\n\n'
    'Your computer will now reboot to start encryption!')
ENCRYPTION_FAILED_MESSAGE = (
    'Encryption was not enabled. Please try again.')
ESCROW_FAILED_MESSAGE = (
    'Encryption was enabled, but escrowing the recovery passphrase failed.\n\n'
    'Please reboot, manually disable FileVault in '
    'System Preferences -> Security & Privacy, '
    'wait for decryption to complete, reboot again, and run CauliflowerVest again.\n')


class Error(Exception):
  """Base error."""


class InputError(Error):
  """Base class for all errors raised for invalid user input."""


class OptionError(Error):
  """Base class for all errors raised by options."""


class FileVaultTool(object):
  """Abstract base class for FileVault tools.

  Child classes should implement the _GetCommand and _GetStdin methods, as well
  as defining values for the attribute list below.

  Attributes:
    NAME: The name of the tool.
    PATH: The filesystem path where the tool's binary is located.
    RETURN_AUTH_FAIL: The int return code used to indicate an authentication
      failure when invoking the tool.
    OUTPUT_PLIST_TOKEN_KEY: The key in the output plist dict for the recovery
      token.
  """

  def __init__(self, username, password):
    self._username = username
    self._password = password

  def EnableEncryption(self):
    """Enable full disk encryption.

    Returns:
      A 2-tuple containing the encrypted volume's UUID, and the recovery token.
    """
    try:
      result_plist = util.GetPlistFromExec(
          self._GetCommand(), stdin=self._GetStdin())
    except util.ExecError as e:
      self._HandleFailure(e)
    return self._HandleResult(result_plist)

  def _GetCommand(self):
    """Return the sequence of args used to invoke the tool in a subprocess."""
    raise NotImplementedError

  def _GetStdin(self):
    """Returns the str data to be passed to the tool subprocess on stdin."""
    raise NotImplementedError

  def _HandleFailure(self, e):
    if e.returncode == self.RETURN_AUTH_FAIL:
      raise InputError(
          'Authentication problem with local account.  Drive NOT encrypted.')
    elif e.returncode != 0:
      logging.error('%s failed with stderr:\n%s', self.NAME, e.stderr)
      raise Error(
          'Problem running %s (exit status = %d)' % (self.NAME, e.returncode))
    else:
      logging.exception('Problem running %s.', self.NAME)
      raise Error('Problem running %s' % self.NAME, e)

  def _HandleResult(self, result_plist):
    """Parse the (plist) output of a FileVault tool."""
    recovery_token = result_plist.get(self.OUTPUT_PLIST_TOKEN_KEY)
    if not recovery_token:
      raise Error('Could not get recovery token!')
    volume_uuid = result_plist.get('LVUUID')
    if not volume_uuid:
      raise Error('Could not get volume UUID!')
    return volume_uuid, recovery_token


class FullDiskEncryptionSetup(FileVaultTool):
  """The Full Disk Encryption Setup (fdesetup) FileVault tool."""

  NAME = 'fdesetup'
  PATH = '/usr/bin/fdesetup'
  RETURN_AUTH_FAIL = 11
  OUTPUT_PLIST_TOKEN_KEY = 'RecoveryKey'

  def _GetCommand(self):
    return ('sudo', '-k', '-S', self.PATH, 'enable', '-user', self._username,
            '-outputplist', '-inputplist')

  def _GetStdin(self):
    # We first need to return the password followed by a newline for the 'sudo'
    # authentication, and then a plist containing the password in a dictionary
    # for 'fdesetup'. (fdesetup either reads the password directly from a tty,
    # or from stdin when passed the '-inputplist' flag.)
    input_plist = plistlib.writePlistToString({'Password': self._password})
    if os.getuid() == 0:
      return input_plist
    return '%s\n%s' % (self._password, input_plist)




def ApplyEncryption(fvclient, username, password):
  """Turn encryption on."""
  # Supply entropy to the system.
  try:
    entropy = util.RetrieveEntropy()
    util.SupplyEntropy(entropy)
  except util.EntropyError as e:
    raise Error('Entropy operations failed: %s' % str(e))

  if os.path.exists(FullDiskEncryptionSetup.PATH):
    # Use "fdesetup" on Mac OS 10.8+ (Mountain Lion).
    logging.debug('Using fdesetup to enable FileVault')
    tool = FullDiskEncryptionSetup(username, password)
  else:
    raise Error('unsupported OS X version (10.7 (Lion) and below)')

  volume_uuid, recovery_token = tool.EnableEncryption()
  fvclient.SetOwner(username)
  return volume_uuid, recovery_token


def CheckEncryptionPreconditions():
  # FileVault2 depends on the presence of a Recovery Partition to
  # enable encryption.
  if not corestorage.GetRecoveryPartition():
    raise OptionError('Recovery partition must exist.')

  # We can't get a recovery passphrase if a keychain is in place.
  if os.path.exists('/Library/Keychains/FileVaultMaster.keychain'):
    raise OptionError(
        'This tool cannot operate with a FileVaultMaster keychain in place.')
