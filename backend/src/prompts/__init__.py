# AIMScribe Prompt Management System
"""
This package provides a structured prompt management system for NER extraction.
All prompts are stored as external .md files with COT reasoning and few-shot examples.
"""

from .loader import PromptLoader

__all__ = ['PromptLoader']
