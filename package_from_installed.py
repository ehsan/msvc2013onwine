# Copyright 2014 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""
From a system-installed copy of the toolchain, packages all the required bits
into a .zip file.

It assumes default install locations for tools, in particular:
- C:\Program Files (x86)\Microsoft Visual Studio 12.0\...
- C:\Program Files (x86)\Windows Kits\8.1\...

1. Start from a fresh Win7 VM image.
2. Install VS Pro. Deselect everything except MFC.
3. Install Windows 8 SDK. Select only the Windows SDK and Debugging Tools for
Windows.
4. Run this script, which will build a <sha1>.zip.

Express is not yet supported by this script, but patches welcome (it's not too
useful as the resulting zip can't be redistributed, and most will presumably
have a Pro license anyway).
"""

import hashlib
import os
import shutil
import sys
import tempfile
import zipfile

import toolchain2013  # pylint: disable=F0401


def BuildFileList():
  result = []

  # Subset of VS corresponding roughly to VC.
  vs_path = r'C:\Program Files (x86)\Microsoft Visual Studio 12.0'
  for path in [
      'VC/atlmfc',
      'VC/bin',
      'VC/crt',
      'VC/include',
      'VC/lib',
      'VC/redist',
      ('VC/redist/x86/Microsoft.VC120.CRT', 'sys32'),
      ('VC/redist/x86/Microsoft.VC120.MFC', 'sys32'),
      ('VC/redist/Debug_NonRedist/x86/Microsoft.VC120.DebugCRT', 'sys32'),
      ('VC/redist/Debug_NonRedist/x86/Microsoft.VC120.DebugMFC', 'sys32'),
      ('VC/redist/x64/Microsoft.VC120.CRT', 'sys64'),
      ('VC/redist/x64/Microsoft.VC120.MFC', 'sys64'),
      ('VC/redist/Debug_NonRedist/x64/Microsoft.VC120.DebugCRT', 'sys64'),
      ('VC/redist/Debug_NonRedist/x64/Microsoft.VC120.DebugMFC', 'sys64'),
      ]:
    src = path[0] if isinstance(path, tuple) else path
    combined = os.path.join(vs_path, src)
    assert os.path.exists(combined) and os.path.isdir(combined)
    for root, _, files in os.walk(combined):
      for f in files:
        final_from = os.path.normpath(os.path.join(root, f))
        if isinstance(path, tuple):
          result.append(
              (final_from, os.path.normpath(os.path.join(path[1], f))))
        else:
          assert final_from.startswith(vs_path)
          dest = final_from[len(vs_path) + 1:]
          if dest.lower().endswith('\\xtree'):
            # Patch for C4702 in xtree. http://crbug.com/346399.
            (handle, patched) = tempfile.mkstemp()
            with open(final_from, 'rb') as unpatched_f:
              unpatched_contents = unpatched_f.read()
            os.write(handle,
                unpatched_contents.replace('warning(disable: 4127)',
                                           'warning(disable: 4127 4702)'))
            result.append((patched, dest))
          else:
            result.append((final_from, dest))

  # Just copy the whole SDK.
  sdk_path = r'C:\Program Files (x86)\Windows Kits\8.1'
  for root, _, files in os.walk(sdk_path):
    for f in files:
      combined = os.path.normpath(os.path.join(root, f))
      to = os.path.join('win8sdk', combined[len(sdk_path) + 1:])
      result.append((combined, to))

  # Generically drop all arm stuff that we don't need.
  return [(f, t) for f, t in result if 'arm\\' not in f.lower()]


def AddEnvSetup(files):
  """We need to generate this file in the same way that the "from pieces"
  script does, so pull that in here."""
  tempdir = tempfile.mkdtemp()
  os.makedirs(os.path.join(tempdir, 'win8sdk', 'bin'))
  toolchain2013.GenerateSetEnvCmd(tempdir, True)
  files.append((os.path.join(tempdir, 'win8sdk', 'bin', 'SetEnv.cmd'),
                'win8sdk\\bin\\SetEnv.cmd'))

if sys.platform != 'cygwin':
  import ctypes.wintypes
  GetFileAttributes = ctypes.windll.kernel32.GetFileAttributesW
  GetFileAttributes.argtypes = (ctypes.wintypes.LPWSTR,)
  GetFileAttributes.restype = ctypes.wintypes.DWORD
  FILE_ATTRIBUTE_HIDDEN = 0x2
  FILE_ATTRIBUTE_SYSTEM = 0x4


def IsHidden(file_path):
  """Returns whether the given |file_path| has the 'system' or 'hidden'
  attribute set."""
  p = GetFileAttributes(file_path)
  assert p != 0xffffffff
  return bool(p & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM))

def GetFileList(root):
  """Gets a normalized list of files under |root|."""
  assert not os.path.isabs(root)
  assert os.path.normpath(root) == root
  file_list = []
  for base, _, files in os.walk(root):
    paths = [os.path.join(base, f) for f in files]
    file_list.extend(x.lower() for x in paths if not IsHidden(x))
  return sorted(file_list)

def MakeTimestampsFileName(root):
  return os.path.join(root, '..', '.timestamps')

def CalculateHash(root):
  """Calculates the sha1 of the paths to all files in the given |root| and the
  contents of those files, and returns as a hex string."""
  file_list = GetFileList(root)

  # Check whether we previously saved timestamps in $root/../.timestamps. If
  # we didn't, or they don't match, then do the full calculation, otherwise
  # return the saved value.
  timestamps_file = MakeTimestampsFileName(root)
  timestamps_data = {'files': [], 'sha1': ''}
  if os.path.exists(timestamps_file):
    with open(timestamps_file, 'rb') as f:
      try:
        timestamps_data = json.load(f)
      except ValueError:
        # json couldn't be loaded, empty data will force a re-hash.
        pass

  matches = len(file_list) == len(timestamps_data['files'])
  if matches:
    for disk, cached in zip(file_list, timestamps_data['files']):
      if disk != cached[0] or os.stat(disk).st_mtime != cached[1]:
        matches = False
        break
  if matches:
    return timestamps_data['sha1']

  digest = hashlib.sha1()
  for path in file_list:
    digest.update(path)
    with open(path, 'rb') as f:
      digest.update(f.read())
  return digest.hexdigest()

def RenameToSha1(output):
  """Determine the hash in the same way that the unzipper does to rename the
  # .zip file."""
  print 'Extracting to determine hash...'
  tempdir = tempfile.mkdtemp()
  old_dir = os.getcwd()
  os.chdir(tempdir)
  rel_dir = 'vs2013_files'
  with zipfile.ZipFile(
      os.path.join(old_dir, output), 'r', zipfile.ZIP_DEFLATED, True) as zf:
    zf.extractall(rel_dir)
  print 'Hashing...'
  sha1 = CalculateHash(rel_dir)
  os.chdir(old_dir)
  shutil.rmtree(tempdir)
  final_name = sha1 + '.zip'
  os.rename(output, final_name)
  print 'Renamed %s to %s.' % (output, final_name)


def main():
  print 'Building file list...'
  files = BuildFileList()

  AddEnvSetup(files)

  if False:
    for f in files:
      print f[0], '->', f[1]
    return 0

  output = 'out.zip'
  if os.path.exists(output):
    os.unlink(output)
  count = 0
  with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED, True) as zf:
    for disk_name, archive_name in files:
      sys.stdout.write('\r%d/%d ...%s' % (count, len(files), disk_name[-40:]))
      sys.stdout.flush()
      count += 1
      zf.write(disk_name, archive_name)
  sys.stdout.write('\rWrote to %s.%s\n' % (output, ' '*50))
  sys.stdout.flush()

  RenameToSha1(output)

  return 0


if __name__ == '__main__':
  sys.exit(main())
