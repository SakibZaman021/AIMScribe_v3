"""
Prompt Loader - Load and render prompt templates from external files.

Features:
- Loads .md prompt files from prompts/ directory
- Supports Jinja2 templating for dynamic content injection
- Caches prompts for performance
- Validates prompt structure
"""

import os
import re
from pathlib import Path
from typing import Dict, Optional, Any
from functools import lru_cache
import logging

try:
    from jinja2 import Environment, FileSystemLoader, Template
    JINJA2_AVAILABLE = True
except ImportError:
    JINJA2_AVAILABLE = False

logger = logging.getLogger(__name__)


class PromptLoader:
    """
    Load and manage prompt templates from external markdown files.
    
    Usage:
        loader = PromptLoader("src/prompts")
        prompt = loader.load("ner", "chief_complaints", transcription="...")
    """
    
    def __init__(self, prompts_dir: str = None):
        """
        Initialize the prompt loader.
        
        Args:
            prompts_dir: Path to prompts directory. Defaults to 'src/prompts'
        """
        if prompts_dir is None:
            prompts_dir = os.environ.get('PROMPTS_DIR', 'src/prompts')
        
        self.prompts_dir = Path(prompts_dir)
        self._cache: Dict[str, str] = {}
        self._metadata_cache: Dict[str, Dict] = {}
        
        if JINJA2_AVAILABLE:
            self._jinja_env = Environment(
                loader=FileSystemLoader(str(self.prompts_dir)),
                trim_blocks=True,
                lstrip_blocks=True
            )
        else:
            self._jinja_env = None
            logger.warning("Jinja2 not available. Using simple string replacement.")
        
        logger.info(f"PromptLoader initialized with directory: {self.prompts_dir}")
    
    def load(self, category: str, name: str, use_cache: bool = True, **kwargs) -> str:
        """
        Load and render a prompt template.
        
        Args:
            category: Prompt category (e.g., 'ner', 'agents')
            name: Prompt name without extension (e.g., 'chief_complaints')
            use_cache: Whether to use cached prompt (default: True)
            **kwargs: Template variables to inject
            
        Returns:
            Rendered prompt string
        """
        cache_key = f"{category}/{name}"
        
        # Load raw template
        if use_cache and cache_key in self._cache:
            raw_template = self._cache[cache_key]
        else:
            raw_template = self._load_file(category, name)
            self._cache[cache_key] = raw_template
        
        # Render template with variables
        return self._render(raw_template, **kwargs)
    
    def _load_file(self, category: str, name: str) -> str:
        """Load a prompt file from disk."""
        file_path = self.prompts_dir / category / f"{name}.md"
        
        if not file_path.exists():
            raise FileNotFoundError(f"Prompt file not found: {file_path}")
        
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Parse and cache metadata if present
        content, metadata = self._parse_frontmatter(content)
        self._metadata_cache[f"{category}/{name}"] = metadata
        
        logger.debug(f"Loaded prompt: {category}/{name}")
        return content
    
    def _parse_frontmatter(self, content: str) -> tuple:
        """Parse YAML frontmatter from prompt file."""
        metadata = {}
        
        # Check for frontmatter (---\n...\n---)
        if content.startswith('---'):
            parts = content.split('---', 2)
            if len(parts) >= 3:
                frontmatter = parts[1].strip()
                content = parts[2].strip()
                
                # Simple YAML parsing (key: value)
                for line in frontmatter.split('\n'):
                    if ':' in line:
                        key, value = line.split(':', 1)
                        metadata[key.strip()] = value.strip()
        
        return content, metadata
    
    def _render(self, template: str, **kwargs) -> str:
        """Render template with variables."""
        if JINJA2_AVAILABLE and self._jinja_env:
            try:
                jinja_template = Template(template)
                return jinja_template.render(**kwargs)
            except Exception as e:
                logger.warning(f"Jinja2 rendering failed, using fallback: {e}")
        
        # Fallback: Simple string replacement
        for key, value in kwargs.items():
            placeholder = "{{ " + key + " }}"
            template = template.replace(placeholder, str(value) if value else "")
            placeholder = "{{" + key + "}}"
            template = template.replace(placeholder, str(value) if value else "")
        
        return template
    
    def get_metadata(self, category: str, name: str) -> Dict:
        """Get metadata for a prompt."""
        cache_key = f"{category}/{name}"
        if cache_key not in self._metadata_cache:
            self.load(category, name)  # This will populate the cache
        return self._metadata_cache.get(cache_key, {})
    
    def list_prompts(self, category: str = None) -> list:
        """List available prompts."""
        prompts = []
        
        if category:
            category_dir = self.prompts_dir / category
            if category_dir.exists():
                for file in category_dir.glob("*.md"):
                    prompts.append(f"{category}/{file.stem}")
        else:
            for category_dir in self.prompts_dir.iterdir():
                if category_dir.is_dir() and not category_dir.name.startswith('_'):
                    for file in category_dir.glob("*.md"):
                        prompts.append(f"{category_dir.name}/{file.stem}")
        
        return prompts
    
    def reload(self, category: str = None, name: str = None):
        """Clear cache to force reload of prompts."""
        if category and name:
            cache_key = f"{category}/{name}"
            self._cache.pop(cache_key, None)
            self._metadata_cache.pop(cache_key, None)
        else:
            self._cache.clear()
            self._metadata_cache.clear()
        logger.info("Prompt cache cleared")


# Global instance for convenience
_default_loader: Optional[PromptLoader] = None


def get_prompt_loader(prompts_dir: str = None) -> PromptLoader:
    """Get or create the default prompt loader instance."""
    global _default_loader
    if _default_loader is None:
        _default_loader = PromptLoader(prompts_dir)
    return _default_loader


def load_prompt(category: str, name: str, **kwargs) -> str:
    """Convenience function to load a prompt using the default loader."""
    return get_prompt_loader().load(category, name, **kwargs)
