# PRIMER — LLM-based Chest X-Ray Audit Tool
# Copyright (C) 2026 Goh Shu Wen
# Licensed under AGPL-3.0-or-later. See LICENSE at the repository root.
import ast
import json
import os
import re
import pandas as pd
import numpy as np
import json_repair

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_CODE_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _env_flag(name):
    return os.environ.get(name, "").lower() in ("true", "1", "yes")


def _structured_parse_method(client):
    """Return the SDK's structured-output parse method.

    openai>=1.40 exposes ``client.chat.completions.parse``; older 1.x releases only
    had ``client.beta.chat.completions.parse`` (removed in openai 2.x).
    """
    completions = client.chat.completions
    if hasattr(completions, "parse"):
        return completions.parse
    return client.beta.chat.completions.parse


def apply_thinking_preference(message_lst):
    """Optionally append Qwen3's ``/no_think`` soft switch to the last user message.

    Opt-in via LLM_DISABLE_THINKING=True (useful for small local Qwen3 models);
    the original message list is never mutated.
    """
    if not _env_flag("LLM_DISABLE_THINKING"):
        return message_lst
    messages = [dict(m) for m in message_lst]
    for message in reversed(messages):
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            if not message["content"].rstrip().endswith("/no_think"):
                message["content"] = message["content"].rstrip() + "\n/no_think"
            break
    return messages


def extract_json_object(text):
    """Extract one JSON object from a model reply.

    Handles reasoning-model output (``<think>...</think>`` blocks, including an
    unterminated block), Markdown code fences and surrounding prose. Raises
    ValueError when no JSON object is present.
    """
    if not isinstance(text, str):
        raise ValueError("empty_llm_response")
    cleaned = _THINK_BLOCK.sub("", text)
    if "<think>" in cleaned.lower():
        # Unterminated reasoning: keep only text after the last closing tag, if any.
        cleaned = cleaned[cleaned.lower().rfind("</think>") + 8:] if "</think>" in cleaned.lower() else ""
    cleaned = _CODE_FENCE.sub("", cleaned.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no_json_object")
    value = json_repair.loads(cleaned[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("no_json_object")
    return value


def _validated(value, response_format):
    """Validate against the Pydantic schema (strict fields stay strict) and return a dict."""
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if hasattr(response_format, "model_validate"):
        return response_format.model_validate(value).model_dump()
    return value


def getLLMJSON(message_lst, client, model_name, response_format, **kwargs):
    """
    Sends a list of messages to a language model (LLM) and returns schema-valid JSON.
    Args:
        message_lst (list): A list of messages to send to the LLM.
        client (OpenAI): An instance of the OpenAI client.
        model_name (str): The name of the model to use.
        response_format (BaseModel): Pydantic model for structured output.

    Returns:
        dict: The parsed JSON object, validated against ``response_format``.

    Raises:
        ValueError: If no schema-valid JSON object is returned after the fallback
        and one repair request.

    Notes:
        - Structured outputs (JSON schema) are tried first; this works with vLLM and
          Ollama's OpenAI-compatible endpoint.
        - The fallback strips reasoning blocks (e.g. Qwen3 ``<think>``), extracts the
          JSON object and validates it against the same schema, so a malformed small
          model reply is rejected instead of silently accepted.
        - Model output is never printed because reports may contain ePHI.
    """
    messages = apply_thinking_preference(message_lst)
    try:
        completion = _structured_parse_method(client)(
            model=model_name,
            messages=messages,
            response_format=response_format,
            **kwargs
        )
        parsed = completion.choices[0].message.parsed
        if parsed is None:
            raise ValueError("structured_output_missing")
        return _validated(parsed, response_format)
    except Exception:
        print("Structured output unavailable; using JSON fallback")

    completion = client.chat.completions.create(
        model=model_name,
        messages=messages,
        temperature=0.2
    )
    resp_content = completion.choices[0].message.content or ""
    try:
        return _validated(extract_json_object(resp_content), response_format)
    except Exception:
        print("Fallback JSON invalid; requesting one repair")

    # Ask the LLM once to repair its own output (no report text is added here).
    debug_messages = apply_thinking_preference([{"role": "user", "content": f"Please fix this JSON and return only the JSON object: {resp_content}"}])
    debug_completion = client.chat.completions.create(
        model=model_name,
        messages=debug_messages,
        temperature=0.2
    )
    try:
        return _validated(extract_json_object(debug_completion.choices[0].message.content or ""), response_format)
    except Exception:
        raise ValueError("No valid JSON object found in the response") from None

def levenshtein_distance(s1, s2):
    """Calculate the Levenshtein distance between two strings."""
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)

    # len(s1) >= len(s2)
    if len(s2) == 0:
        return len(s1)

    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]

def closestFinding(txt, padchest_findings):
    """
    Given a text, find the closest finding from the padchest findings list.
    """
    # Find the closest finding in padchest_findings
    closest_finding = None
    min_distance = float('inf')

    for finding in padchest_findings:
        distance = levenshtein_distance(txt, finding)
        if distance < min_distance:
            min_distance = distance
            closest_finding = finding

    return closest_finding

def encode_findings(findings, padchest_findings, tubes_lines_findings, diagnoses):
    """
    Encode textual findings data to numerical indices for processing.
    This function takes the text values from the findings, devices, and diagnoses dictionaries and
    maps their textual values to corresponding indices.
    Other values like temporal and certainty are left alone.

    Args:
        findings (dict): A dictionary containing chest X-ray findings with the three dicts
            'findings_all', 'devices_all', and 'diagnoses_all', derived from semanticExtractionCXR().
        padchest_findings (dict): A dictionary mapping findings to their indices. (Pass in padchest_findings)
        tubes_lines_findings (dict): A dictionary mapping tube and line findings to their indices. (Pass in tubes_lines_findings)
        diagnoses (dict): A dictionary mapping diagnoses to their indices. (Pass in diagnoses)

    Returns:
        dict: A copy of the input dictionary with text values replaced by their
            corresponding numerical indices (if available). The original structure is preserved.

    Note:
        The function uses closestFinding() when a finding is not found in padchest_findings
        to map to the closest matching finding.
    """

    # Make a deep copy to avoid modifying the original
    result = findings.copy()
    temporal_list = ['new', 'better', 'worse', 'stable', 'not mentioned']
    uncertainty_list = ['certain', 'uncertain', 'not mentioned']
    device_placement_list = ['satisfactory', 'suboptimal', 'malpositioned', 'not mentioned']

    # Convert each finding to its index in padchest_findings
    if 'findings_all' in result:
        findings_list = list(padchest_findings)  # Convert dict keys to list
        for one_finding in result['findings_all']:
            if one_finding['finding'] in findings_list:
                one_finding['finding'] = findings_list.index(one_finding['finding'])
            else:
                # If the finding is not in the list, find the closest one
                closest_finding = closestFinding(one_finding['finding'], padchest_findings)
                if closest_finding:
                    one_finding['finding'] = findings_list.index(closest_finding)

            if one_finding['temporal'] in temporal_list:
                one_finding['temporal'] = temporal_list.index(one_finding['temporal'])
            if one_finding['uncertainty'] in uncertainty_list:
                one_finding['uncertainty'] = uncertainty_list.index(one_finding['uncertainty'])

    # Convert each device to its index in tubes_lines_findings
    if 'devices_all' in result:
        devices_list = list(tubes_lines_findings)  # Convert dict keys to list
        for one_device in result['devices_all']:
            if one_device['medical_device'] in devices_list:
                one_device['medical_device'] = devices_list.index(one_device['medical_device'])
            if one_device['placement'] in device_placement_list:
                one_device['placement'] = device_placement_list.index(one_device['placement'])

    # Convert each diagnosis to its index in diagnoses
    if 'diagnoses_all' in result:
        diagnoses_list = list(diagnoses)  # Convert dict keys to list
        for one_diagnosis in result['diagnoses_all']:
            if one_diagnosis['diagnosis'] in diagnoses_list:
                one_diagnosis['diagnosis'] = diagnoses_list.index(one_diagnosis['diagnosis'])
            if one_diagnosis['temporal'] in temporal_list:
                one_diagnosis['temporal'] = temporal_list.index(one_diagnosis['temporal'])

    return result

# Function to safely parse a string representation of a list of dictionaries
def parse_list_dict(val):
    """
    Safely parse a string representation of a list of dictionaries into a Python list.
    """
    # If already a list or NumPy array, return it as a list.
    if isinstance(val, (list, np.ndarray)):
        return list(val)

    # If the value is not a string, return empty list
    if not isinstance(val, str):
        return []

    # Check for common "null" values
    if val in (None, 'None'):
        return []

    # Check for a missing value using pd.isna
    if pd.isna(val):
        return []

    # Try to parse using ast.literal_eval
    try:
        return ast.literal_eval(val)
    except Exception:
        try:
            cleaned_str = val.replace("'", '"').replace("None", "null")
            return json.loads(cleaned_str)
        except Exception:
            print(f"Failed to parse: {val}")
            return []
