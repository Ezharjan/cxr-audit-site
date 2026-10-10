# PRIMER — LLM-based Chest X-Ray Audit Tool
# Copyright (C) 2026 Goh Shu Wen
# Licensed under AGPL-3.0-or-later. See LICENSE at the repository root.
import ast
import json
import pandas as pd
import numpy as np
import json_repair

def getLLMJSON(message_lst, client, model_name, response_format, **kwargs):
    """
    Sends a list of messages to a language model (LLM) and attempts to parse the response as JSON.
    Args:
        message_lst (list): A list of messages to send to the LLM.
        client (OpenAI): An instance of the OpenAI client.
        model_name (str): The name of the model to use.
        response_format (BaseModel): Pydantic model for structured output.

    Returns:
        dict: The parsed JSON object from the LLM response.

    Raises:
        ValueError: If no valid JSON object is found in the response after multiple attempts.

    Notes:
        - If the initial response cannot be parsed as JSON, the function will attempt to extract a valid JSON object using regex.
        - If regex extraction fails, the function will ask the LLM for help in debugging the JSON.
        - If all attempts to parse the response as JSON fail, a ValueError is raised.
        - With the new JSON schema format, json_repair shouldn't be necessary anymore.
    """

    try:
        # Use structured outputs with OpenAI API
        completion = client.beta.chat.completions.parse(
            model=model_name,
            messages=message_lst,
            response_format=response_format,
            **kwargs
        )

        # Previous params:
        # NO LONGER NEEDED because everything is defined in the model
        #    temperature=0.7,
        #    top_p=0.8,
        #    presence_penalty=1.5,
        #    extra_body={
        #        "top_k": 20,
        #        "thinking_token_budget": 500,
        #    },

        # Extract the parsed content
        resp_json = completion.choices[0].message.parsed

        # Convert Pydantic model to dict if needed
        if hasattr(resp_json, 'model_dump'):
            return resp_json.model_dump()
        elif hasattr(resp_json, 'dict'):
            return resp_json.dict()
        else:
            return resp_json

    except Exception as e:
        print("Structured output unavailable; using JSON fallback")
        # Fallback to regular completion
        completion = client.chat.completions.create(
            model=model_name,
            messages=message_lst,
            temperature=0.2
        )

        resp_content = completion.choices[0].message.content

        try:
            # Try to parse the response content as JSON
            resp_json = json_repair.loads(resp_content)
            # Return the parsed JSON object
            return resp_json
        except json.JSONDecodeError:
            # If JSON decoding fails, print an error message and the response content
            print(f'JSONDecodeError!')


        # Attempt to find a valid JSON object within the response content using regex
        match = re.search(r'\{.*\}', resp_content, re.DOTALL)
        if match:
            # If a valid JSON object is found, parse it
            intermediate = match.group()
        else:
            # If no valid JSON object is found, print an error message
            print('No valid JSON object found in the response. Retrying...')

        # Ask the LLM for help
        debug_messages = [{"role": "user", "content": f"Please fix this JSON: {resp_content}"}]
        debug_completion = client.chat.completions.create(
            model=model_name,
            messages=debug_messages,
            temperature=0.2
        )
        intermediate = debug_completion.choices[0].message.content

        try:
            # Try to parse the corrected response content as JSON
            resp_json = json_repair.loads(intermediate)
            # Return the parsed JSON object
            return resp_json
        except json.JSONDecodeError:
            # If JSON decoding fails again, we give up
            print(f'JSONDecodeError Again!')

            raise ValueError("No valid JSON object found in the response")

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
