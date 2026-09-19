"""
AIMScribe AI Backend - NER Extractor Module
Handles clinical entity extraction using LangChain and COT Prompts.

Optimized with Sequential Batching aligned to Clinical Workflow:
  - Batch 1 (History):     chief_complaints, drug_history, additional_notes
  - Batch 2 (Assessment):  examination, investigations, diagnosis
  - Batch 3 (Plan):        medications, advice, follow_up

This respects Azure OpenAI rate limits (3 concurrent requests per batch)
while following the natural flow of a medical consultation.
"""

import logging
import json
import time
from typing import Dict, List, Optional, Any, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

from langchain_openai import AzureChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.output_parsers import JsonOutputParser

from config import settings
from prompts.loader import PromptLoader, get_prompt_loader

logger = logging.getLogger(__name__)

# Workers per batch (3 modules per batch, respects Azure rate limits)
BATCH_WORKERS = 3


class NERUnavailable(RuntimeError):
    """
    NER cannot run because Azure is not configured.

    Raised per job rather than at startup, so the absence of an optional AI
    feature never stops audio being captured, uploaded or archived.
    """


class NERExtractor:
    """
    Extracts clinical entities using COT prompts and few-shot examples.

    Uses Sequential Batching Strategy aligned with clinical workflow:
    1. History Phase:     What patient reports (complaints, drug history, notes)
    2. Assessment Phase:  Doctor's examination and findings
    3. Plan Phase:        Treatment decisions (medications, advice, follow-up)
    """

    def __init__(self):
        # The client is built on first use, not here.
        #
        # AzureChatOpenAI validates credentials in its constructor. Building it
        # eagerly meant that with no Azure key the worker raised during startup,
        # exited, and was restarted forever by Docker - so a missing NER key took
        # down transcription, the queue consumer and archiving too. NER is one
        # feature; it should fail on its own, per job, and leave the rest running.
        self._llm = None
        self.prompt_loader = get_prompt_loader()

    @property
    def llm(self):
        if self._llm is None:
            if not settings.azure_ner_api_key or not settings.azure_ner_endpoint:
                raise NERUnavailable(
                    "Azure NER is not configured: set AZURE_NER_ENDPOINT and "
                    "AZURE_NER_API_KEY. Recording, upload and archiving are "
                    "unaffected; only entity extraction is disabled.")

            logger.info(f"Initializing NER LLM with endpoint: {settings.azure_ner_endpoint}")
            logger.info(f"Using deployment: {settings.azure_ner_deployment}, API version: {settings.azure_api_version}")

            self._llm = AzureChatOpenAI(
                azure_endpoint=settings.azure_ner_endpoint,
                api_key=settings.azure_ner_api_key,
                api_version=settings.azure_api_version,
                deployment_name=settings.azure_ner_deployment,
                # temperature=0.1,  # GPT-5.2 only supports default temperature (1)
                max_completion_tokens=4000,  # GPT-5.2 requires max_completion_tokens instead of max_tokens
                timeout=60,           # Request timeout in seconds
                max_retries=2,        # LangChain internal retries (we have our own retry logic)
            )
        return self._llm

    @staticmethod
    def is_configured() -> bool:
        return bool(settings.azure_ner_api_key and settings.azure_ner_endpoint)

    def extract_all(
        self,
        transcription: str,
        patient_context: Dict = None,
        previous_medications: List[Dict] = None
    ) -> Dict[str, Any]:
        """
        Run full NER pipeline with Sequential Batching (Clinical Workflow).

        Batches are executed sequentially to respect Azure rate limits:
          Batch 1 → wait → Batch 2 → wait → Batch 3

        Args:
            transcription: Full conversation text
            patient_context: Database baseline data (demographics, etc.)
            previous_medications: List of meds from prev visit (for "continue same" logic)

        Returns:
            Complete NER JSON
        """
        start_time = time.time()
        logger.info("Starting NER extraction pipeline (sequential batching mode)...")

        # 1. Initialize result structure with patient context
        result = self._init_structure(patient_context)

        # 2. Prepare medication context for batch 3
        hist_context = json.dumps(previous_medications, ensure_ascii=False) if previous_medications else None

        # 3. Define batches aligned with clinical workflow
        # Each batch: List of (module_name, result_key, kwargs)

        # BATCH 1: History Phase - Patient's self-reported information
        # These come first in a consultation (symptoms, previous meds, family history)
        batch_1_history = [
            ("chief_complaints", "Chief Complaints (English)", {}),
            ("drug_history", "_drug_history", {}),
            ("additional_notes", "_additional_notes", {}),
        ]

        # BATCH 2: Assessment Phase - Doctor's examination and clinical findings
        # Doctor examines after hearing complaints, orders tests, forms diagnosis
        batch_2_assessment = [
            ("examination", "_examination", {}),
            ("investigations", "Investigations (English)", {}),
            ("diagnosis", "Diagnosis (English)", {}),
        ]

        # BATCH 3: Plan Phase - Treatment decisions
        # Made after diagnosis: prescriptions, lifestyle advice, follow-up
        batch_3_plan = [
            ("medications", "Medications", {"previous_medications": hist_context}),
            ("advice", "Advice (Bengali)", {}),
            ("follow_up", "Follow Up (Bengali)", {}),
        ]

        # 4. Execute batches sequentially
        extraction_results = {}

        batches = [
            ("History", batch_1_history),
            ("Assessment", batch_2_assessment),
            ("Plan", batch_3_plan),
        ]

        for batch_name, batch_tasks in batches:
            batch_start = time.time()
            logger.info(f"Executing {batch_name} batch ({len(batch_tasks)} modules)...")

            batch_results = self._execute_batch(batch_tasks, transcription)
            extraction_results.update(batch_results)

            batch_elapsed = time.time() - batch_start
            logger.info(f"{batch_name} batch completed in {batch_elapsed:.2f}s")

        # 5. Merge results into final structure
        self._merge_results(result, extraction_results)

        elapsed = time.time() - start_time
        logger.info(f"NER extraction completed in {elapsed:.2f}s (sequential batching)")
        return result

    def _execute_batch(
        self,
        tasks: List[Tuple[str, str, Dict]],
        transcription: str
    ) -> Dict[str, Any]:
        """
        Execute a batch of extraction tasks in parallel.

        Args:
            tasks: List of (module_name, result_key, kwargs)
            transcription: The transcription text

        Returns:
            Dict of result_key -> extracted_data
        """
        results = {}

        with ThreadPoolExecutor(max_workers=BATCH_WORKERS) as executor:
            future_to_task = {
                executor.submit(
                    self._extract_module,
                    task[0],  # module_name
                    transcription,
                    **task[2]  # kwargs
                ): task
                for task in tasks
            }

            for future in as_completed(future_to_task):
                task = future_to_task[future]
                module_name, result_key, _ = task

                try:
                    data = future.result()
                    results[result_key] = data
                    logger.debug(f"Module {module_name} completed successfully")
                except Exception as e:
                    logger.error(f"Module {module_name} failed: {e}")
                    # Set safe default based on expected type
                    results[result_key] = {} if module_name in ["examination", "follow_up"] else []

        return results

    def _merge_results(self, result: Dict, extraction_results: Dict) -> None:
        """
        Merge extraction results into the final NER structure.

        Args:
            result: The final NER structure to populate
            extraction_results: Raw extraction results from all batches
        """
        # Direct mappings (top-level fields)
        result["Chief Complaints (English)"] = extraction_results.get("Chief Complaints (English)", [])
        result["Medications"] = extraction_results.get("Medications", [])
        result["Diagnosis (English)"] = extraction_results.get("Diagnosis (English)", [])
        result["Investigations (English)"] = extraction_results.get("Investigations (English)", [])
        result["Advice (Bengali)"] = extraction_results.get("Advice (Bengali)", [])
        result["Follow Up (Bengali)"] = extraction_results.get("Follow Up (Bengali)", {})

        # Special handling: Examination (O/E and S/E)
        exam_data = extraction_results.get("_examination", {})
        if exam_data:
            result["Examination (English)"]["O/E (English)"] = exam_data.get("O/E (English)", {})
            result["Examination (English)"]["S/E (English)"] = exam_data.get("S/E (English)", {})

        # Special handling: Drug History (nested under Examination)
        drug_history = extraction_results.get("_drug_history", [])
        if drug_history:
            result["Examination (English)"]["Drug History (English)"] = drug_history

        # Special handling: Additional Notes (flatten object to list)
        notes_data = extraction_results.get("_additional_notes", {})
        if notes_data:
            flat_notes = []
            if notes_data.get("Family History"):
                flat_notes.extend([f"Family: {x}" for x in notes_data["Family History"]])
            if notes_data.get("Prior Episodes"):
                flat_notes.extend([f"History: {x}" for x in notes_data["Prior Episodes"]])
            if notes_data.get("Allergies"):
                flat_notes.extend([f"Allergy: {x}" for x in notes_data["Allergies"]])
            if notes_data.get("Social History"):
                flat_notes.extend([f"Social: {x}" for x in notes_data["Social History"]])
            result["Examination (English)"]["Additional Notes (English)"] = flat_notes

    def _extract_module(
        self,
        prompt_name: str,
        transcription: str,
        max_retries: int = 3,
        base_delay: float = 1.0,
        **kwargs
    ) -> Any:
        """
        Run a single extraction module with retry logic.

        Args:
            prompt_name: Name of the prompt template
            transcription: The transcription text
            max_retries: Maximum number of retry attempts
            base_delay: Base delay for exponential backoff (seconds)
            **kwargs: Additional template variables

        Returns:
            Extracted data (dict or list depending on module)
        """
        last_error = None

        for attempt in range(max_retries + 1):
            try:
                # Load and render prompt
                system_msg = self.prompt_loader.load(
                    "ner",
                    prompt_name,
                    transcription=transcription,
                    **kwargs
                )

                # Call LLM
                messages = [
                    SystemMessage(content=system_msg)
                ]

                response = self.llm.invoke(messages)
                content = response.content.strip()

                # Clean markdown code blocks if present
                if "```json" in content:
                    content = content.split("```json")[1].split("```")[0].strip()
                elif "```" in content:
                    content = content.split("```")[1].split("```")[0].strip()

                # Parse JSON
                try:
                    data = json.loads(content)
                    return data
                except json.JSONDecodeError:
                    logger.error(f"Failed to parse JSON for {prompt_name}: {content[:100]}...")
                    return [] if prompt_name not in ["examination", "follow_up"] else {}

            except Exception as e:
                last_error = e
                error_str = str(e).lower()
                error_type = type(e).__name__

                # Log detailed error information for debugging
                logger.warning(
                    f"Module {prompt_name} error - Type: {error_type}, "
                    f"Message: {str(e)[:200]}"
                )

                # Check if it's a rate limit or transient error
                is_retryable = any(x in error_str for x in [
                    "rate limit", "429", "timeout", "connection",
                    "temporary", "overloaded", "503", "500", "ssl",
                    "reset", "refused", "unreachable"
                ])

                if is_retryable and attempt < max_retries:
                    # Exponential backoff: 1s, 2s, 4s
                    delay = base_delay * (2 ** attempt)
                    logger.warning(
                        f"Module {prompt_name} failed (attempt {attempt + 1}/{max_retries + 1}), "
                        f"retrying in {delay:.1f}s: {error_type}"
                    )
                    time.sleep(delay)
                else:
                    # Non-retryable error or max retries reached
                    logger.error(f"Module {prompt_name} - Non-retryable or max retries: {error_type}")
                    break

        logger.error(f"Module {prompt_name} failed after {max_retries + 1} attempts: {type(last_error).__name__}: {last_error}")
        # Return safe empty defaults based on expected type
        return {} if prompt_name in ["examination", "follow_up"] else []

    def _init_structure(self, ctx: Dict = None) -> Dict:
        """Initialize empty JSON structure with patient demographics."""
        ctx = ctx or {}
        demographics = ctx.get('demographics', {})

        return {
            "Patient Info (English)": {
                "Name (English)": demographics.get('name', ''),
                "Age (English)": demographics.get('age', ''),
                "Gender (English)": demographics.get('gender', ''),
                "Blood Group (English)": demographics.get('blood_group', ''),
                "Last Visit Date (English)": ctx.get('last_visit_date', ''),
                "Consultation Date (English)": ""
            },
            "Chief Complaints (English)": [],
            "Examination (English)": {
                "O/E (English)": {
                    "ANEMIA. (English)": "",
                    "DEHYDRATION. (English)": "",
                    "JAUNDICE. (English)": "",
                    "EDEMA. (English)": ""
                },
                "S/E (English)": {
                    "Heart (English)": "",
                    "Lung (English)": "",
                    "Abdomen (English)": ""
                },
                "Drug History (English)": [],
                "Additional Notes (English)": []
            },
            "Investigations (English)": [],
            "Diagnosis (English)": [],
            "Medications": [],
            "Advice (Bengali)": [],
            "Follow Up (Bengali)": {
                "Next Consultation Date (Bengali)": ""
            }
        }
