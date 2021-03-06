#!/usr/bin/env python
# Copyright 2010 Google Inc. All Rights Reserved.
"""A module to load all vfs handler plugins."""

# pylint: disable=unused-import
import platform

# These import populate the VFSHandler registry
from grr_response_client.vfs_handlers import files
from grr_response_client.vfs_handlers import sleuthkit

# pylint: disable=g-import-not-at-top
if platform.system() == "Windows":
  from grr_response_client.vfs_handlers import registry
