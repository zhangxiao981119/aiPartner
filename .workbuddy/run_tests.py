# -*- coding: utf-8 -*-
"""测试驱动：逐文件跑 pytest，纯文本摘要写 result.txt（绕 PowerShell 中文乱码）。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pytest

TESTS = ["test_予安_StateEngine.py", "test_予安_MoodEngineV2.py",
         "test_予安_TopicEngine.py", "test_予安_NavPlugin.py",
         "test_予安_ConsentStore.py", "test_予安_CompanionManager.py",
         "test_予安_Prompt.py", "test_予安_Orchestrator.py",
         "test_予安_LLM.py", "test_予安_Version.py",
         "test_予安_VoiceTemplate.py", "test_予安_version_guard.py",
         "test_予安_MemoryStore.py"]
TESTS = [os.path.join(ROOT, t) for t in TESTS]
os.chdir(ROOT)

code = pytest.main(["-q", "-p", "no:cacheprovider", "--color=no",
                    "--junitxml=" + os.path.join(HERE, "junit.xml")] + TESTS)

import xml.etree.ElementTree as ET
total = failed = errors = 0
junit = os.path.join(HERE, "junit.xml")
if os.path.exists(junit):
    root = ET.parse(junit).getroot()
    for suite in root.iter("testsuite"):
        total += int(suite.get("tests", 0))
        failed += int(suite.get("failures", 0))
        errors += int(suite.get("errors", 0))

with open(os.path.join(HERE, "result.txt"), "w", encoding="ascii", errors="replace") as f:
    f.write("EXIT=%d TOTAL=%d FAILED=%d ERRORS=%d\n" % (code, total, failed, errors))
sys.exit(code)
