# PRIMER — LLM-based Chest X-Ray Audit Tool
# Copyright (C) 2026 Goh Shu Wen
# Licensed under AGPL-3.0-or-later. See LICENSE at the repository root.
from string import Template
from pydantic import BaseModel, Field, StrictBool

sys_role_msg = '''You are a helpful medical assistant.
Your task is to classify chest X-ray reports according to the following grading system:
Grading (R):
1 = normal without any findings
2 = normal variant, does NOT require follow up
3 = abnormal, non-urgent follow-up required
4 = abnormal, potentially important finding
5 = critical, urgent follow-up required

Grade 5 should be used for newly-diagnosed, critical, life threatening findings, including:
- Pneumothorax
- Aortic dissection
- Mediastinal emphysema
- Pneumoperitoneum
- Portal venous air
- Dilated bowel loops
- Significant tube or line misplacement e.g. malpositioned NGT

Grade 5 should also be used for newly-diagnosed, unexpected, life-changing findings, including:
- New lesions that are suspicious for malignancy
- New fracture
- New large volume pleural effusions
- New aneurysm

Grade 3 is for other abnormal findings, in particular:
- Atelectasis (new or stable)
- Calcification (new or stable)
- Cardiomegaly (new or stable)
- Fibrosis (new or stable)
- Scarring (new or stable)
- Nodule (stable or nonspecific, not concerning for malignancy)

Grade 4 should be used for any finding that has significant clinical impact (i.e. would require treatment) \
that is new or worsening (save for those to be graded as 2, as below).

Please provide your answer in a valid JSON, according to the defined format. Do not include any additional text or explanations.'''

################################
### AGENT 1: SEMIALGO METHOD ###
################################

prompt_get_findings = Template('''# Your Task
# This Python code snippet defines a template string using the `Template` class from the `string`
# module. The template string contains detailed instructions for a task related to analyzing X-ray
# reports.
Below is a sample of an X-ray report, enclosed in <report> tags. 

<report>
${rpt}
</report>

Please generate a list of positive findings from the report, and whether the finding is new, better, worse or stable. 
Also, please specify if any medical devices (tubes & lines) are present in the report and their placement, whether it is satisfactory or malpositioned.
Medical devices should not be in "findings" but in "devices". If there is no medical device, please return an empty list for "devices_all" ([]).
Mastectomy, surgical clips and surgical plates should be considered as findings and not as medical devices.

Please choose the finding from this list. \
Use only the term that best fits the finding. \
Do not return a finding that is not in this list. \
List all positive findings only. Negative findings should not be included.

Here is the list of findings: ${padchest_findings_str}

Here is a list of medical devices (tubes & lines): ${tubes_lines_findings_str}

Here is a list of overarching diagnoses: ${diagnoses_str}

Here is a description of the parameters:
For the anatomical findings
- finding: Pathological finding mentioned in the report, using the list above. Do not return a finding that is not in this list. If you think the finding is not in this list, return the closest one from the list.
- location: Location of the finding, e.g. right lower zone
- system: System of the body where the finding is located -- Lung, Pleura, Heart, Mediastinum, MSK, Abdomen
- temporal: new, better, worse, stable or not mentioned
- uncertainty: certain, uncertain, not mentioned
If there are no findings, return an empty list for "findings_all" ([]).

For the medical devices (tubes & lines)
- medical_device: Name of the tube/line, using the list above. Do not return a device that is not in this list.
- placement: Placement of the tube/line -- satisfactory, suboptimal (abnormal but no urgent action is required), malpositioned (urgent repositioning or removal is required) or not mentioned
If there are no medical devices, return an empty list for "devices_all" ([]).

For the diagnoses
- diagnosis: Overarching diagnosis suggested in the report, using the list above. Do not return a diagnosis that is not in this list.
    - For example, if "...suggestive of infection" is mentioned, return "pneumonia" as the diagnosis.
    - For example, if "...may be related to infection" is mentioned, return "pneumonia" as the diagnosis.
    - For example, if "...malignancy cannot be excluded" is mentioned, return "malignancy" as the diagnosis.
    - For example, if "...P1 for..." is mentioned, return "P1" as the diagnosis.
- temporal: new, better, worse, stable or not mentioned
If there are no diagnoses, return an empty list for "diagnoses_all" ([]).

# Finer Points
If the heart size is not accurately assessed, this should NOT be considered as a finding.
If the only finding is that there is no consolidation or pleural effusion with no other findings, this should be considered as a normal report with no finding. \
Even if "no consolidation" or "no pleural effusion" is qualified, for example "no confluent consolidation", \
"no large pleural effusion" or "no frank pleural effusion" \
this should be considered as a normal report with no finding.
If a finding is "probably", "suggestive of", "likely" or any other similar phrase that indicates a low uncertainty, \
this should be considered as a finding with "certain" uncertainty.
If a finding is "possibly", "may represent", "could be", "cannot be excluded" \
or any other similar phrase that indicates a high uncertainty, this should be considered as a finding with "uncertain" uncertainty.
A rotated or suboptimally inspired film can be considered as "suboptimal study".
Findings written in pleural should be considered as singular (e.g. "pleural effusions" as "pleural effusion", "nodular opacities" as "nodular opacity", "granulomas" as "granuloma").

If the report suggests the possibility of a diagnosis of pneumonia, infection, or suggests correlation with infective markers, this diagnosis should be raised.
If the report suggests the possibility of a diagnosis of tuberculosis, atypical infection or mycobacterial infection, this diagnosis should be raised.
If the report suggests the possibility of a tumour, malignancy, or neoplasm, this diagnosis should be raised.
    - If a CT thorax was suggested for an opacity, the diagnosis is "malignancy".
If the reporting radiologist indicated at the end that the report is "P1", this diagnosis should be raised.
    - This can come in the format "P1 for X", or "Dr xx was informed at the time of reporting".
    - This should not be raised if there is no indication that the report is P1, for example if findings are all stable or have resolved.
    
If the report suggests that a diagnosis "cannot be excluded", it should be considered as a positive diagnosis.
If the report suggests that a diagnosis from the diagnoses list is "possible", it should be considered as a positive diagnosis.
''')

class PositiveFindings(BaseModel):
    class OneFinding(BaseModel):
        finding: str
        location: str
        system: str
        temporal: str
        uncertainty: str
    
    class Devices(BaseModel):
        medical_device: str
        placement: str
    
    class Diagnoses(BaseModel):
        diagnosis: str
        temporal: str

    findings_all: list[OneFinding]
    devices_all: list[Devices]
    diagnoses_all: list[Diagnoses]
    
#############################
### METHOD 2: LLM GRADING ###
#############################

prompt_grade_llm = Template('''# Your Task
Below is a chest X-ray report, enclosed in <report> tags. \
Please grade the report on a scale of 1 to 6.
Return the highest grade of the findings in the report.

# Finer Points
Any finding that has significant clinical impact that is new or worsening should be graded as 4 \
(save for those to be graded as 2, as below).
If the chronicity of a finding is not mentioned, it should be considered as new.
If a finding is "probably" X, it should be considered as X.
If there is no finding in the body of the report, even though there is a comparison, \
this should be graded as 1.
The presence of a comparison should NOT affect the grading of the report.

Unfolded aorta, vascular calcifications, chronic bone findings and an unfolded thoracic aorta do NOT need any follow up and should be graded as 2.
Benign findings also do NOT require follow up and should be graded as 2.
Patient rotation and suboptimal effort do NOT require follow up and should be graded as 2.
If there is suboptimal technique with no other finding, this should be graded as 2.

Pleural thickening, atelectasis and fibrosis should be grade 3.
Isolated cardiomegaly can be graded as 3.
Pulmonary calcifications like granulomas, pleural plaques and calcified adenopathy, if these are the only findings, \
should be graded as 3.
Pulmonary nodules should be graded as 3 even if they are stable, unless they are concerning for malignancy, \
in which case they should be graded as 4.

Potential infection/consolidation and possible fluid overload or pulmonary congestion should be graded as 4.
Patchy opacities, airspace opacities and hazy opacities should be graded as 4 for new pneumonia/infection.
A small pleural effusion can be graded as 3, but significant ones (moderate/severe) should be graded as 4.
If the report suggests the possibility of a diagnosis of pneumonia, infection, or suggests correlation with infective markers, this should be graded as 4.
If the report suggests the possibility of a diagnosis of tuberculosis, atypical infection or mycobacterial infection, this should be graded as 4.
    - If the report suggests that tuberculosis is stable, this should be graded as 3.
If the report suggests the possibility of a tumour, malignancy, or neoplasm, this should be graded as 4.
    - If the report suggests that the tumour is stable, this should be graded as 3.
If the reporting radiologist indicated at the end that the report is "P1", this should be graded as 5.
    - This can come in the format "P1 for X", or "Dr xx was informed at the time of reporting".

Any line present should be at least grade 3 (unless malpositioned, in which case it should be grade 6)
    - A nasogastric tube in a satisfactory position can be grade 3
    - Any other line should be at least grade 4 (because they're seen in critically ill pts)
    - Malpositioned lines are all grade 5.

# Output format
Please provide your answer in a valid JSON with the following format:
{
    "grade": int (1 to 5, integer)
}

# Examples
Here are some examples of CXR reports and their grades:
Example: "The cardiac silhouette is unremarkable. No focal lung lesion or consolidation is seen."
Grade: 1

Example: "The heart is enlarged. There is a small pleural effusion in the right lower zone."
Grade: 4

Example: "No consolidation or pleural effusion is seen. The heart size is not accurately assessed."
Grade: 2

Example: "Comparison is made with the prior study dated 12 August 2022. The heart is not enlarged. No focal lung lesion is seen."
Grade: 1

# Report to grade
Please grade this CXR report:
<report>
${rpt}
</report>
''')

class cxrGrade(BaseModel):
    grade: int = Field(ge=1, le=6, strict=True)
    

# SUPPLEMENT: LUNIT FINDINGS INCL TB
prompt_get_lunit_findings = Template('''# Your Task
Below is a sample of an X-ray report, enclosed in <report> tags. \
It has already been graded as "abnormal". Your job is instead to output whether specific findings are present or absent.
If the finding is in one of the following categories, please indicate <finding>_llm = true.
The categories are:
- Atelectasis
- Calcification
- Cardiomegaly
- Consolidation
- Fibrosis
- Mediastinal Widening
- Nodule
- Pleural Effusion
- Pneumoperitoneum
- Pneumothorax

If the finding is tuberculosis, please indicate tb = true.

# Output format
Please provide your answer in a valid JSON with the following format:
{
    "atelectasis_llm": bool,
    "calcification_llm": bool,
    "cardiomegaly_llm": bool,
    "consolidation_llm": bool,
    "fibrosis_llm": bool,
    "mediastinal_widening_llm": bool,
    "nodule_llm": bool,
    "pleural_effusion_llm": bool,
    "pneumoperitoneum_llm": bool,
    "pneumothorax_llm": bool,
    "tb": bool
}

# Report to analyse
<report>
${rpt}
</report>
''')

prompt_get_lunit_findings_testing = Template('''# Your Task
Below is a sample of an X-ray report, enclosed in <report> tags. \
It has already been graded as "abnormal". Your job is instead to output whether specific findings are present or absent.
If the finding is in one of the following categories, please indicate <finding>_llm = true.
The categories are:
- Atelectasis
- Calcification
- Cardiomegaly
- Consolidation
- Fibrosis
- Mediastinal Widening
- Nodule
- Pleural Effusion
- Pneumoperitoneum
- Pneumothorax

If the finding is tuberculosis, please indicate tb = true.

# Finer points
The presence of the following are considered positive for atelectasis:
- Fibrotic band
- Bulla
- Bronchiectasis
- Post radiotherapy changes
- Fissure thickening

The presence of the following are considered positive for calcification:
- (Calcified) granuloma
- Tuberculosis sequelae
- Asbestos signs
- (Calcified) Pleural plaques/thickening
- Calcified adenopathy
- Calcified fibroadenoma

The presence of the following are considered positive for cardiomegaly:
- Pericardial effusion
- Pulmonary artery hypertension

The presence of the following are considered positive for consolidation:
- Abscess
- Empyema
- Cavitation
- Pulmonary fibrosis
- Bronchiectasis
- Pneumonia
- Ground-glass opacity
- Alveolar opacity
- Interstitial opacity
- Miliary opacities
- Reticulonodular/reticular opacities
- Pulmonary oedema
- Vascular redistribution
- Carcinomatosis lymphangitis
- Post-radiation changes

The presence of the following are considered positive for fibrosis:
- Fibrotic band
- Bulla
- Bronchiectasis
- Volume loss
- Post radiotherapy changes

The presence of the following are considered positive for mediastinal widening:
- Aortic aneurysm
- Mediastinal mass
- Mediastinal enlargement

The presence of the following are considered positive for nodule:
- Pulmonary mass
- Pulmonary absces
- Empyema
- Cavitation
- Granuloma
- Miliary opacities
- Reticulonodular opacities (not simply reticular)
- Lung metastases
- Loculated pleural effusion
- Pleural mass
- Hilar congestion
- Mediastinal mass

The presence of the following are considered positive for pleural effusion:
- Empyema
- Hydropneumothorax
- Loculated pleural effusion
- Costophrenic angle blunting
- Pleural thickening
- Pleural mass
- Asbestos signs
- Pleural plaques

# Output format
Please provide your answer in a valid JSON with the following format:
{
    "atelectasis_llm": bool,
    "calcification_llm": bool,
    "cardiomegaly_llm": bool,
    "consolidation_llm": bool,
    "fibrosis_llm": bool,
    "mediastinal_widening_llm": bool,
    "nodule_llm": bool,
    "pleural_effusion_llm": bool,
    "pneumoperitoneum_llm": bool,
    "pneumothorax_llm": bool,
    "tb": bool
}

# Report to analyse
<report>
${rpt}
</report>
''')

class LunitFindings(BaseModel):
    atelectasis_llm: StrictBool
    calcification_llm: StrictBool
    cardiomegaly_llm: StrictBool
    consolidation_llm: StrictBool
    fibrosis_llm: StrictBool
    mediastinal_widening_llm: StrictBool
    nodule_llm: StrictBool
    pleural_effusion_llm: StrictBool
    pneumoperitoneum_llm: StrictBool
    pneumothorax_llm: StrictBool
    tb: StrictBool

################################
### METHOD 3: HYBRID GRADING ###
################################

prompt_grade_hybrid = Template('''# Your Task
Below is a chest X-ray report, enclosed in <report> tags. \
Please grade the report on a scale of 1 to 6.
I've also given you a grade from your assistant; please consider this grade when grading the report.
Return the highest grade of the findings in the report.
Also tell me why the assistant's grade is appropriate if you agree, or not appropriate if you disagree.

# Grading Instructions
In addition to the general grading criteria, here are some specific findings to consider and their associated grades:
${padchest_findings_str}
${diagnoses_str}

# Finer Points
Any finding that has significant clinical impact that is new or worsening should be graded as 4 \
(save for those to be graded as 2, as below).
If the chronicity of a finding is not mentioned, it should be considered as new.
If a finding is "probably" X, it should be considered as X.
The presence of a comparison should NOT affect the grading of the report.

Unfolded aorta, vascular calcifications, chronic bone findings and an unfolded thoracic aorta do NOT need any follow up and should be graded as 2.
Patient rotation and suboptimal effort do NOT require follow up and should be graded as 2.
If there is suboptimal technique with no other finding, this should be graded as 2.
If the heart size is not accurately assessed, this should be graded as 2.

Pleural thickening, atelectasis and fibrosis should be grade 3, even if stable.
Isolated cardiomegaly can be graded as 3.
Pulmonary calcifications like granulomas, pleural plaques and calcified adenopathy, if these are the only findings, \
should be graded as 3, even if stable.
Pulmonary nodules should be graded as 3 even if they are stable, unless they are concerning for malignancy, \
in which case they should be graded as 4.

Potential infection/consolidation and possible fluid overload or pulmonary congestion should be graded as 4.
Patchy opacities, airspace opacities and hazy opacities should be graded as 4 for new pneumonia/infection.
A small pleural effusion can be graded as 3, but significant ones (moderate/severe) should be graded as 4.
If the report suggests the possibility of a diagnosis of pneumonia, infection, or suggests correlation with infective markers, this should be graded as 4.
If the report suggests the possibility of a diagnosis of tuberculosis, atypical infection or mycobacterial infection, this should be graded as 4.
    - If the report suggests that tuberculosis is stable, this should be graded as 3.
If the report suggests the possibility of a tumour, malignancy, or neoplasm, this should be graded as 4.
    - If the report suggests that the tumour is stable, this should be graded as 3.
If the reporting radiologist indicated at the end that the report is "P1", this should be graded as 5.
    - This can come in the format "P1 for X", or "Dr xx was informed at the time of reporting".

Any line present should be at least grade 3 (unless malpositioned, in which case it should be grade 6)
    - A nasogastric tube in a satisfactory position can be grade 3
    - Any other line should be at least grade 4 (because they're seen in critically ill pts)
    - Malpositioned lines are all grade 5.

# Output format
Please provide your answer in a valid JSON with the following format:
{
    "grade": int (Your grade, 1 to 5, integer; if you agree with the assistant's grade you can return the same grade as the assistant),
    "explanation": str (a brief explanation of why the assistant's grade is appropriate or not appropriate)
}

# Examples
Here are some examples of CXR reports and their grades:
Example: "The cardiac silhouette is unremarkable. No focal lung lesion or consolidation is seen."
Grade: 1

Example: "The heart is enlarged. There is a small pleural effusion in the right lower zone."
Grade: 4

Example: "No consolidation or pleural effusion is seen. The heart size is not accurately assessed."
Grade: 2

Example: "Comparison is made with the prior study dated 12 August 2022. The heart is not enlarged. No focal lung lesion is seen."
Grade: 1

# Report to grade
Please grade this CXR report:
<report>
${rpt}
</report>

Assistant's grade: ${grade_semialgo}
''')

class cxrGradeHybrid(BaseModel):
    grade: int
    explanation: str
    
################################
### METHOD 4: JUDGE GRADING ###
################################

prompt_judge_grading = Template('''Your task is to grade the following CXR report, enclosed in three backticks (```). \
You are given two automated grades (algorithm and LLM). \
Please tell me which of the two automated grades is more appropriate for this report, and explain your answer. 
Please grade the report on a scale of 1 to 6.

Below are some guidelines:
Patient rotation and suboptimal effort do NOT require follow up and should be graded as 2.
If there is suboptimal technique with no other finding, this should be graded as 2.
If the heart size is not accurately assessed, this should be graded as 2.

The presence of a comparison should NOT affect the grading of the report.
If there the report is normal but there is a comparison, this should be graded as 1.

Please provide your answer in a valid JSON with the following format:
{
    "grade": 1-5, what you think is the most appropriate grade,
    "choice": 0-3 - 
        0 = both are not appropriate
        1 = algorithm grade is better
        2 = LLM grade is better
        3 = both grades are the same
    "explanation": your explanation of your responses above
}

Please grade this CXR report:
${rpt}

The algorithm grade is ${grade_algo} and the LLM grade is ${grade_llm}.
''')

prompt_judge_grading_ext = Template('''Your task is to grade the following CXR report, enclosed in three backticks (```). \
You are given two grades (algorithm and manual). \
Please tell me which of the two grades is more appropriate for this report, and explain your answer. 
Please grade the report on a scale of 1 to 5.

Below are some guidelines:
Patient rotation and suboptimal effort do NOT require follow up and should be graded as 2.
If there is suboptimal technique with no other finding, this should be graded as 2.
If the heart size is not accurately assessed, this should be graded as 2.

The presence of a comparison should NOT affect the grading of the report.
If there the report is normal but there is a comparison, this should be graded as 1.

Please provide your answer in a valid JSON with the following format:
{
    "grade": 1-5, what you think is the most appropriate grade,
    "choice": 0-2 - 
        0 = both are not appropriate
        1 = algorithm grade is better
        2 = manual grade is better
        3 = both grades are the same
    "explanation": your explanation of your responses above
}

Please grade this CXR report:
${rpt}

The algorithm grade is ${grade_auto} and the manual grade is ${grade_manual}.
''')

class cxrGradeJudge(BaseModel):
    grade: int
    choice: int
    explanation: str