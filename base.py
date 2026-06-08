#! /usr/bin/python3

"""Base functions for Jenny"""

import logging
import re
import os.path

from jinja2 import Environment, FileSystemLoader, select_autoescape

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

jenv = Environment(
    loader=FileSystemLoader(os.path.join(os.path.dirname(__file__), "templates")),
    autoescape=select_autoescape(),
)

urlsep = ","
_sep1 = "--"
_sep2 = "--"


class PackageEntry:
    def __init__(self, name, version, distribution, component, architecture):
        self.name = name
        self.version = version
        self.distribution = distribution
        self.component = component
        self.architecture = architecture

    def sortablestr(self) -> str:
        return "%(name)s/%(component)s/%(architecture)s/%(version)s" % self.__dict__

    def comparablestr(self) -> str:
        return "%(name)s/%(component)s/%(architecture)s" % self.__dict__

    def __repr__(self) -> str:
        return "%(name)s/%(version)s/%(architecture)s" % self.__dict__


def dec2str(distribution: str, environment: str, component: str) -> str:
    return f"%s{_sep1}%s{_sep2}%s" % (distribution, environment, component)


def str2dec(s: str) -> tuple[str, str, str]:
    if m := re.search(f"(.*){_sep1}([a-z]+){_sep2}(.*)", s):
        return (m.group(1), m.group(2), m.group(3))
    return None


def str2de(s: str) -> tuple[str, str]:
    if m := re.search(f"(.*){_sep1}([a-z]+)", s):
        return (m.group(1), m.group(2))
    return None


def de2str(distribution: str, environment: str) -> str:
    return f"%s{_sep1}%s" % (distribution, environment)
