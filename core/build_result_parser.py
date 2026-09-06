# SPDX-License-Identifier: MIT
"""
core/build_result_parser.py

Parses raw Docker execution output into structured BuildResult.

Classification priority:
  1. Exit code (language-agnostic, most reliable)
  2. Structured reports — JUnit XML (surefire), Jest JSON, pytest output
  3. Pattern matching on stdout/stderr (fallback)

Build statuses:
  BUILD_SUCCESS       — exit 0, all tests passed
  COMPILE_ERROR       — compilation failed (LLM can fix)
  TEST_FAILURE        — tests ran, some failed (LLM can fix)
  TEST_INFRA_FAILURE  — tests need live infra (connection refused etc.) — skip
  DEPENDENCY_MISSING  — artifact not in Nexus — stop, notify
  BUILD_TIMEOUT       — exceeded timeout — stop, notify
  UNKNOWN_ERROR       — catch-all, retry once with verbose output
"""

from __future__ import annotations

import io
from core.logger import logger
import re
import tarfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Literal, Optional

BuildStatus = Literal[
    "BUILD_SUCCESS",
    "COMPILE_ERROR",
    "TEST_FAILURE",
    "TEST_INFRA_FAILURE",
    "INFRA_FAILURE",
    "DEPENDENCY_MISSING",
    "BUILD_TIMEOUT",
    "UNKNOWN_BUILD_PATTERN",
    "UNKNOWN_ERROR",
]


@dataclass
class TestDetails:
    total:        int
    passed:       int
    failed:       int
    failed_tests: list[str] = field(default_factory=list)
    source:       str = ""          # "surefire_xml" | "jest_stdout" | "pytest_stdout" | "regex"


@dataclass
class PhaseResult:
    status:          BuildStatus
    exit_code:       int            = 0
    command:         str            = ""
    image:           str            = ""
    duration_secs:   int            = 0
    output_tail:     str            = ""
    error_lines:     list[str]      = field(default_factory=list)
    test_details:    Optional[TestDetails] = None
    missing_artifact: str           = ""
    cache_degraded:  bool           = False   # builder dep-cache disabled/unwritable


# ── Pattern sets (language-agnostic where possible) ──────────────────────────

_INFRA_PATTERNS = [
    r"Connection refused",
    r"Could not connect to",
    r"Unable to acquire.*?connection",
    r"ECONNREFUSED",
    r"datasource.*?not available",
    r"Address already in use",
    r"BindException",
    r"redis\.exceptions\.ConnectionError",
    r"kafka\.errors\.NoBrokersAvailable",
]

_DEP_PATTERNS = [
    r"Could not resolve dependencies",
    r"Could not find artifact",
    r"Could not transfer artifact",
    r"Package not found",
    r"Cannot find module '",
    r"No module named '",
    r"unknown import path",
    r"error\[E\d+\]: unresolved import",
    r"PKGBUILDDIR.*not found",
]

_TEST_OUTPUT_SIGNALS = re.compile(
    r"(Tests run:|passed|failed|FAILED|AssertionError|FAIL:|\.\.\. FAIL|pytest|ok\s+\d+\s+tests)",
    re.IGNORECASE,
)

# Cache-degradation signals — the builder's persistent dependency cache was
# disabled or not writable (UID/ownership mismatch), so pip/npm/etc. fell back to
# slow uncached downloads.  Detecting these lets us surface CACHE_DEGRADED instead
# of letting the run masquerade as a plain BUILD_TIMEOUT.  Language-spread.
_CACHE_DEGRADED_PATTERNS = [
    r"cache has been disabled",                       # pip
    r"is not owned or is not writable",               # pip ownership heuristic
    r"Check the permissions and owner of that directory",  # pip
    r"npm warn.*?EACCES",                             # npm cache perms
    r"Your cache folder contains root-owned files",   # npm
]
_CACHE_DEGRADED_RE = re.compile("|".join(_CACHE_DEGRADED_PATTERNS), re.IGNORECASE)


def _detect_cache_degraded(output: str) -> bool:
    return bool(output) and bool(_CACHE_DEGRADED_RE.search(output))


class BuildResultParser:

    def parse_compile(
        self, exit_code: int, output: str, command: str, image: str, duration: int
    ) -> PhaseResult:
        status, extras = self._classify(exit_code, output, phase="compile")
        degraded = _detect_cache_degraded(output)
        logger.info(
            f"build_result_parser [compile]: exit={exit_code} status={status} "
            f"duration={duration}s image={image} missing={extras.get('missing_artifact','')!r}"
            + (" cache_degraded=True" if degraded else "")
        )
        return PhaseResult(
            status=status,
            exit_code=exit_code,
            command=command,
            image=image,
            duration_secs=duration,
            output_tail=output[-4000:],
            error_lines=extras.get("error_lines", []),
            missing_artifact=extras.get("missing_artifact", ""),
            cache_degraded=degraded,
        )

    def parse_test(
        self,
        exit_code: int,
        output: str,
        command: str,
        image: str,
        duration: int,
        surefire_bytes: bytes | None = None,   # pre-extracted tar bytes from container
    ) -> PhaseResult:
        # Try structured parsers first
        test_details = None
        if surefire_bytes:
            test_details = self._parse_surefire(surefire_bytes)
        if test_details is None and "pytest" in command:
            test_details = self._parse_pytest(output)
        if test_details is None and ("jest" in command or "npm test" in command):
            test_details = self._parse_jest(output)

        if test_details is not None:
            status = "BUILD_SUCCESS" if test_details.failed == 0 else "TEST_FAILURE"
            logger.info(
                f"build_result_parser [test]: exit={exit_code} status={status} "
                f"source={test_details.source} total={test_details.total} "
                f"passed={test_details.passed} failed={test_details.failed} "
                f"duration={duration}s image={image}"
            )
            return PhaseResult(
                status=status,
                exit_code=exit_code,
                command=command,
                image=image,
                duration_secs=duration,
                output_tail=output[-4000:],
                error_lines=self._extract_error_lines(output) if status != "BUILD_SUCCESS" else [],
                test_details=test_details,
                cache_degraded=_detect_cache_degraded(output),
            )

        # Fallback to generic classification
        status, extras = self._classify(exit_code, output, phase="test")
        degraded = _detect_cache_degraded(output)
        logger.info(
            f"build_result_parser [test/fallback]: exit={exit_code} status={status} "
            f"duration={duration}s image={image}"
            + (" cache_degraded=True" if degraded else "")
        )
        return PhaseResult(
            status=status,
            exit_code=exit_code,
            command=command,
            image=image,
            duration_secs=duration,
            output_tail=output[-4000:],
            error_lines=extras.get("error_lines", []),
            test_details=extras.get("test_details"),
            missing_artifact=extras.get("missing_artifact", ""),
            cache_degraded=degraded,
        )

    # ── Generic classifier ─────────────────────────────────────

    def _classify(self, exit_code: int, output: str, phase: str) -> tuple[BuildStatus, dict]:
        if exit_code == 0:
            return "BUILD_SUCCESS", {}
        if exit_code == -1:
            return "BUILD_TIMEOUT", {}
        if exit_code == -2:            # workspace_builder DockerException sentinel (daemon down)
            return "INFRA_FAILURE", {"error_lines": self._extract_error_lines(output)}

        for pattern in _INFRA_PATTERNS:
            if re.search(pattern, output, re.IGNORECASE):
                logger.debug(f"build_result_parser: TEST_INFRA_FAILURE matched pattern={pattern!r}")
                return "TEST_INFRA_FAILURE", {}

        for pattern in _DEP_PATTERNS:
            if re.search(pattern, output, re.IGNORECASE):
                artifact = self._extract_artifact_name(output)
                logger.warning(
                    f"build_result_parser: DEPENDENCY_MISSING matched pattern={pattern!r} "
                    f"artifact={artifact!r}"
                )
                return "DEPENDENCY_MISSING", {"missing_artifact": artifact}

        if _TEST_OUTPUT_SIGNALS.search(output):
            failed_tests = self._extract_failed_tests(output)
            logger.debug(
                f"build_result_parser: TEST_FAILURE via regex fallback "
                f"failed_count={len(failed_tests)}"
            )
            return "TEST_FAILURE", {
                "error_lines":  self._extract_error_lines(output),
                "test_details": TestDetails(
                    total=0, passed=0, failed=len(failed_tests),
                    failed_tests=failed_tests, source="regex",
                ),
            }

        if phase == "compile":
            return "COMPILE_ERROR", {"error_lines": self._extract_error_lines(output)}

        return "UNKNOWN_ERROR", {"error_lines": self._extract_error_lines(output)}

    # ── Structured parsers ─────────────────────────────────────

    def _parse_surefire(self, surefire_bytes: bytes) -> TestDetails | None:
        """Parse JUnit XML from pre-extracted surefire-reports tar bytes."""
        try:
            xml_files = self._extract_xml_files_from_tar(surefire_bytes)
        except Exception as exc:
            logger.debug(f"build_result_parser: surefire tar extract failed: {exc}")
            return None

        if not xml_files:
            logger.debug("build_result_parser: surefire tar contained no XML files")
            return None

        logger.debug(f"build_result_parser: surefire parsing {len(xml_files)} XML file(s)")

        total = passed = failed = 0
        failed_tests: list[str] = []

        for xml_str in xml_files:
            try:
                root = ET.fromstring(xml_str)
                t = int(root.get("tests", 0))
                f = int(root.get("failures", 0)) + int(root.get("errors", 0))
                total  += t
                failed += f
                passed += t - f
                for tc in root.findall(".//testcase"):
                    if tc.find("failure") is not None or tc.find("error") is not None:
                        cls  = tc.get("classname", "")
                        name = tc.get("name", "")
                        failed_tests.append(f"{cls}.{name}" if cls else name)
            except ET.ParseError:
                pass

        return TestDetails(
            total=total, passed=passed, failed=failed,
            failed_tests=failed_tests, source="surefire_xml",
        )

    def _parse_pytest(self, output: str) -> TestDetails | None:
        m = re.search(
            r"(\d+) passed(?:,\s*(\d+) failed)?|(\d+) failed(?:,\s*(\d+) passed)?",
            output
        )
        if not m:
            logger.debug("build_result_parser: pytest summary line not found in output")
            return None
        logger.debug(f"build_result_parser: pytest matched summary: {m.group()!r}")
        if m.group(1):
            passed = int(m.group(1))
            failed = int(m.group(2) or 0)
        else:
            failed = int(m.group(3))
            passed = int(m.group(4) or 0)
        failed_tests = re.findall(r"FAILED\s+([\w/.:]+)", output)
        return TestDetails(
            total=passed + failed, passed=passed, failed=failed,
            failed_tests=failed_tests, source="pytest_stdout",
        )

    def _parse_jest(self, output: str) -> TestDetails | None:
        m = re.search(r"Tests:\s+(?:(\d+) failed,\s*)?(\d+) passed", output)
        if not m:
            logger.debug("build_result_parser: jest summary line not found in output")
            return None
        logger.debug(f"build_result_parser: jest matched summary: {m.group()!r}")
        failed = int(m.group(1) or 0)
        passed = int(m.group(2))
        failed_tests = re.findall(r"✕\s+(.+)", output) or re.findall(r"×\s+(.+)", output)
        return TestDetails(
            total=passed + failed, passed=passed, failed=failed,
            failed_tests=failed_tests, source="jest_stdout",
        )

    # ── Utility ────────────────────────────────────────────────

    def _extract_xml_files_from_tar(self, raw: bytes) -> list[str]:
        results = []
        try:
            with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
                for member in tf.getmembers():
                    if member.name.endswith(".xml"):
                        f = tf.extractfile(member)
                        if f:
                            results.append(f.read().decode(errors="replace"))
        except Exception:
            pass
        return results

    def _extract_error_lines(self, output: str) -> list[str]:
        return [
            line.strip() for line in output.splitlines()
            if re.search(r"\b(error|ERROR|FAILED|Exception|cannot find|undefined)\b", line)
        ][:30]

    def _extract_failed_tests(self, output: str) -> list[str]:
        patterns = [
            r"FAILED\s+([\w.]+)",           # Maven / pytest
            r"✕\s+(.+)",                    # Jest
            r"--- FAIL:\s+(\S+)",           # Go
            r"test\s+(\S+)\s+\.\.\.\s+FAIL",  # Rust
        ]
        tests: list[str] = []
        for p in patterns:
            tests.extend(re.findall(p, output))
        return list(dict.fromkeys(tests))[:20]  # dedup, cap 20

    def _extract_artifact_name(self, output: str) -> str:
        m = (
            re.search(r"Could not find artifact\s+([\w.:]+)", output) or
            re.search(r"Could not resolve\s+([\w.:]+)", output) or
            re.search(r"Package not found:\s+(\S+)", output) or
            re.search(r"Cannot find module '([^']+)'", output) or
            re.search(r"No module named '([^']+)'", output)
        )
        return m.group(1) if m else ""