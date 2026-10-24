#!/usr/bin/env python3
"""Mutant guard: reads the event and decides nothing. selftest.py must FAIL on it."""
import sys

sys.stdin.read()
sys.exit(0)
