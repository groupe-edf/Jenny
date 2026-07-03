#! /usr/bin/python3

# SPDX-FileCopyrightText: © 2026-Present EDF
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from config import load_config
from cli import jennycli

load_config()


if __name__ == "__main__":
    load_config()
    jennycli()
