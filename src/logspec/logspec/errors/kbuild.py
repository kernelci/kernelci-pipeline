# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2024 Collabora Limited
# Author: Ricardo Cañuelo <ricardo.canuelo@collabora.com>

import logging
import os
import re

from ..utils.defs import *
from .error import Error


# Kbuild error classes


class KbuildCompilerError(Error):
    """Models the information extracted from a compiler/linker error.
    """
    def __init__(self, script=None, target=None):
        """Object initializer.

        Parameters:
          script (str): Kbuild script and location where it failed
          target (str): source or object file that caused
              the compiler error
        """
        super().__init__()
        self.script = script
        self.target = target
        self.src_file = ""
        self.location = ""
        self.error_type = "kbuild.compiler"
        self._signature_fields.extend([
            'src_file',
            'target',
        ])

    def _parse_linker_error(self, text):
        """Parses a linker error message and saves the source file and
        error summary.

        Updates the object fields with the error information if an error
        was found.

        Returns:
          True if a linker error was found. False otherwise.

        """
        logging.debug(f"[_parse_linker_error()] target: {self.target}, text: {text}")
        if self.target != os.path.basename(self.target):
            # Target is an absolute path
            regex = re.compile(rf'ld: .*?(?P<obj_file>{self.target}\.\w+)')
        else:
            # Target is a relative path
            regex = re.compile('ld: (?P<obj_file>.*?):')

        match = regex.search(text)
        if match:
            self.error_type += f".linker_error"
            src_file = os.path.basename(match.group('obj_file'))
            src_dir = os.path.dirname(match.group('obj_file'))
            src_file_name = os.path.splitext(src_file)[0]
            src_file_ext = os.path.splitext(src_file)[1].strip('.')
            match = re.search(fr'(?P<src_file>{src_file_name}\.\w+):(?P<location>[^: ]+): (?P<message>.*?)\n', text)
            if match:
                self.location = match.group('location')
                self.src_file = os.path.join(src_dir, match.group('src_file'))
                self.error_summary = match.group('message').strip()
            return True
        # Catch any other linker error
        match = re.search('ld: (?P<message>.*)', text)
        if match:
            self.error_type += f".linker_error"
            self.error_summary = match.group('message')
            return True
        return False

    def _parse_compiler_error(self, text):
        """Parses a compiler error message and saves the source file and
        error summary.

        An error message may be a compiler error or warning. Linker
        errors are excluded.

        Updates the object fields with the error information if an error
        was found.

        Returns:
          True if a compiler error was found. False otherwise.
        """
        logging.debug(f"[_parse_compiler_error()] text: {text}")
        # Bail out if the error in the text looks like a linker error
        if re.search('ld: ', text):
            return False

        # Get error type and summary
        match = re.search(r'.*?(?P<type>error|warning): (?P<message>.*?)\n', text)
        if match:
            self.error_type += f".{match.group('type')}"
            self.error_summary = match.group(0).strip()

        # Get source file and location
        # Try to get the source file and location from the error
        # message, search for the target file stem
        target = os.path.splitext(self.target)[0]
        logging.debug(f"[_parse_compiler_error()] target: {target}")
        match = re.search(fr'(?P<src_file>{target}(\.\w+)?):(?P<location>\d+)', text)
        if match:
            self.src_file = match.group('src_file')
            self.location = match.group('location')
            return True
        else:
            # Try again matching only the basename
            target = os.path.splitext(os.path.basename(self.target))[0]
            match = re.search(fr'(?P<src_file>{target}(\.\w+)?):(?P<location>\d+)', text)
            if match:
                target_dir = os.path.dirname(self.target)
                self.src_file = os.path.join(target_dir, match.group('src_file'))
                self.location = match.group('location')
                return True
        return False

    def _parse_compiler_error_line(self, text):
        """Searches for and parses compiler errors/warnings that are
        contained in a single line (see the regex below for details).

        Returns:
          The end position of the error in text

        Example:

        drivers/../link_factory.c:743:1: error:
        the frame size of 1040 bytes is larger than 1024 bytes
        [-Werror=frame-larger-than=]
        """
        file_pattern = os.path.splitext(self.target)[0]
        match = re.search(f'^.*?(?P<src_file>{file_pattern}.*?):(?P<location>.*?): (?P<type>.*?): (?P<message>.*?)\n',
                          text, flags=re.MULTILINE)
        if match:
            self._report = text[match.start():]
            self.src_file = match.group('src_file')
            self.location = match.group('location')
            self.error_type += f".{match.group('type')}"
            self.error_summary = match.group('message')
            return len(text)
        return 0

    def _parse_compiler_error_block(self, text):
        """Parses compiler errors that are laid out in a block of lines.
        It searches for a line that contains the target string, then
        looks for the error block starting after it, where the error
        block starts with the first unindented line and ends before the
        Make error line.

        Returns:
          The end position of the error in text.

        Example:

        In file included from ./arch/arm/include/asm/atomic.h:16,
                         from ./include/linux/atomic.h:7,
                         from ./include/asm-generic/bitops/lock.h:5,
                         from ./arch/arm/include/asm/bitops.h:245,
                         from ./include/linux/bitops.h:63,
                         from ./include/linux/log2.h:12,
                         from kernel/bounds.c:13:
        ./arch/arm/include/asm/cmpxchg.h: In function ‘__cmpxchg’:
        ./arch/arm/include/asm/cmpxchg.h:167:12: error: implicit declaration of function ‘cmpxchg_emu_u8’ [-Werror=implicit-function-declaration]
          167 |   oldval = cmpxchg_emu_u8((volatile u8 *)ptr, old, new);
              |            ^~~~~~~~~~~~~~
        cc1: some warnings being treated as errors
        """
        def _find_error_block(text, target):
            """Given a <text> containing one or many compiler error
            outputs and a build <target>, searches for all the error
            blocks in the text related to the target and returns the
            start position of the last one.
            """
            target_stem = os.path.splitext(target)[0]
            # Get the start position of the block to parse (ie. the
            # block where the Make target file appears that's closest to
            # the Make failure)
            matches = re.finditer(f'^.*{target_stem}.*$', text, flags=re.MULTILINE)
            # Get the last match (the last block, if many were found)
            try:
                *_, match = matches
            except ValueError:
                return None
            return match.start()

        # Get the error text block
        logging.debug(f"[_parse_compiler_error_block()] target: {self.target}")
        block_start = _find_error_block(text, self.target)
        if not block_start:
            return 0
        self._report = text[block_start:]
        logging.debug(f"[_parse_compiler_error_block()] block: {text[block_start:]}")
        parsers = [
            self._parse_compiler_error,
            self._parse_linker_error,
        ]
        for parser in parsers:
            if parser(self._report):
                break
        return len(text)

    def _parse(self, text):
        """Parses a log fragment looking for a compiler error for a
        specific file (self.target) and updates the object with the
        extracted information.

        Strategy 1: Search for lines that look like a compiler
        error/warning message.

        Strategy 2: Search for a line that contains the target string,
        then look for the error block starting after it, where the error
        block starts with the first unindented line and continues until
        the end of the text.

        Parameters:
          text (str): the text log containing the compiler error

        Returns the position in `text' where the error block ends (if
        found).
        """
        parse_strategies = [
            self._parse_compiler_error_line,
            self._parse_compiler_error_block,
        ]

        parse_end_pos = 0
        for strat in parse_strategies:
            parse_end_pos = strat(text)
            if parse_end_pos:
                break
        if self.location:
            self._signature_fields.append('location')
        return parse_end_pos


class KbuildProcessError(Error):
    """Models the information extracted from a kbuild error caused by a
    script, configuration or other runtime error.
    """
    def __init__(self,  script=None, target=None):
        """Object initializer.

        Parameters:
          script (str): Kbuild script and location where it failed
          target (str): Kbuild target that failed
        """
        super().__init__()
        self.script = script
        self.target = target
        self._signature_fields.extend([
            'script',
            'target',
        ])

    def _parse(self, text):
        """Parses a log fragment looking for a generic Kbuild error
        and updates the object with the extracted information.

        Strategy: Look for lines containing "***".

        Parameters:
          text (str): the text log containing the error

        Returns the position in `text' where the error block ends (if
        found).
        """
        end = 0
        self.error_type = "kbuild.make"
        match = re.finditer(r'\*\*\*.*', text)
        summary_strings = []
        for m in match:
            self._report += f"{m.group(0)}\n"
            summary_strings.append(m.group(0).strip('*\n '))
            end = m.end()
        if summary_strings:
            self.error_summary = " ".join([string for string in summary_strings if string])
        return end


class KbuildModpostError(Error):
    """Models the information extracted from a kbuild error in the
    modpost target.
    """
    def __init__(self,  script=None, target=None):
        """Object initializer.

        Parameters:
          script (str): Kbuild script and location where it failed
          target (str): Kbuild target that failed
        """
        super().__init__()
        self.script = script
        self.target = target
        self._signature_fields.extend([
            'script',
            'target',
        ])

    def _parse(self, text):
        """Parses a log fragment looking for a modpost Kbuild error
        and updates the object with the extracted information.

        Strategy: look for lines containing "ERROR: modpost: ".

        Parameters:
          text (str): the text log containing the modpost error

        Returns the position in `text' where the error block ends (if
        found).
        """
        end = 0
        self.error_type = "kbuild.modpost"
        match = re.finditer(r'ERROR: modpost: (?P<message>.*)', text)
        summary_strings = []
        for m in match:
            self._report += f"{m.group(0)}\n"
            summary_strings.append(m.group('message'))
            end = m.end()
        if summary_strings:
            self.error_summary = " ".join(summary_strings)
        return end


class KbuildGenericError(Error):
    """Models the information extracted from a Kbuild error that doesn't
    have a known type. This is meant to be used to catch errors that
    look like a known Kbuild error but for which we don't have enough
    info to really tell which type it is.
    """
    def __init__(self,  script=None, target=None):
        """Object initializer.

        Parameters:
          script (str): Kbuild script and location where it failed
          target (str): Kbuild target that failed
        """
        super().__init__()
        self.script = script
        self.target = target
        self._signature_fields.extend([
            'script',
            'target',
        ])

    def _parse(self, text):
        """Parses a log fragment looking for a generic Kbuild error
        and updates the object with the extracted information.

        Strategy: if a target was specified, search for errors _after_
        the first appearance of the `target' string in the log. To
        search for these errors, look for unindented lines.

        Parameters:
          text (str): the text log containing the modpost error

        Returns the position in `text' where the error block ends (if
        found).
        """
        self.error_type = "kbuild.other"
        end = 0
        if self.target:
            match = re.search(self.target, text)
            if not match:
                return end
            summary_strings = []
            match = re.finditer(r'^[^\s]+.*$', text[match.end():], flags=re.MULTILINE)
            for m in match:
                current_match = m.group()
                self._report += f"{current_match}\n"
                # Error type: '***'-prefix block:
                # Extract summary from error message
                if current_match.startswith('***'):
                    summary_strings.append(current_match.strip('*\n '))
                else:
                    # Error type (catch-all): any line containing
                    # 'error:'. Use that as the summary
                    generic_error_match = re.search(r'.*error:.*', current_match)
                    if generic_error_match:
                        summary_strings.append(generic_error_match.group())
                end = m.end()
            if summary_strings:
                self.error_summary = " ".join([string for string in summary_strings if string])
        return end


class KbuildUnknownError(Error):
    def __init__(self, text):
        super().__init__()
        self.error_type = "kbuild.unknown"
        self.error_summary = text
        self._report = text


# Error detection utility functions


def _is_object_file(target):
    """Returns True if `target' looks like an object or "output" file
    according to a list of known extensions. Returns False otherwise.
    """
    known_extensions = [
        '.o',
        '.s',
    ]
    base, ext = os.path.splitext(target)
    if not ext or ext not in known_extensions:
        return False
    return True


def _is_other_compiler_target(target, text):
    """Returns True if `target` can be identified to be a compiler
    target file based on its appearance in `text`. Returns False
    otherwise.
    """
    target_base = os.path.splitext(os.path.basename(target))[0]
    match = re.search(rf'{target_base}(\.\w+)?:', text)
    if match:
        return True
    else:
        return False


def _is_kbuild_target(target):
    """Returns True if `target' looks like a Kbuild target. Returns
    False otherwise.
    """
    known_targets = [
        'modules',
        'Module.symvers',
    ]
    if target in known_targets:
        return True
    return False


def find_kbuild_error(text):
    """Find a kbuild error in a text segment.

    Currently supported:
      - compiler errors (C)
      - Make / Kbuild runtime errors

    Parameters:
      text (str): the log or text fragment to parse

    Returns:
    If an error report was found, it returns a dict containing:
      'error': specific error object containing the structured error info
      'end': position in the text right after the parsed block
    None if no error report was found.
    """
    end = 0
    match = re.search(r'make.*?: \*\*\* (?P<error_str>.*)', text)
    if not match:
        return None
    error_str = match.group('error_str')
    start = match.start()
    end = match.end()
    match = re.search(r'\[(?P<script>.*?): (?P<target>.*?)\] Error', error_str)
    if match:
        script = match.group('script')
        target = match.group('target')
        logging.debug(f"[find_kbuild_error] script: {script}, target: {target}")
        error = None
        # Kbuild error classification
        if _is_object_file(target) or _is_other_compiler_target(target, text[:start]):
            error = KbuildCompilerError(script=script, target=target)
        elif 'modpost' in script:
            error = KbuildModpostError(script=script, target=target)
        elif _is_kbuild_target(target):
            error = KbuildProcessError(script=script, target=target)
        else:
            # Catch-all condition for non-specific errors
            error = KbuildGenericError(script=script, target=target)
        text = text[:start]
        error.parse(text)
    else:
        # Unrecognized error, these are marked as unknown and not parsed
        error = KbuildUnknownError(error_str)
    return {
        'error': error,
        '_end': end,
    }
