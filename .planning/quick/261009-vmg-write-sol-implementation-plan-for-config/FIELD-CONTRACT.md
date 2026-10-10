# PRIME upload contract

Physical CXRStudy fields and types remain unchanged. Configuration controls interpretation, never storage or upload permission. Only PRIME is enabled.

| Field(s) | Meaning/unit | Upload and completeness | Existing-row enrichment | Dependencies |
|---|---|---|---|---|
| accession_no | Exact nonnegative signed bigint identifier; UI anon_id | Required; aliases ACCESSION_NO, anon_id, Accession Number; no floats, leading zeros or coercion | Identity only | Merge, persistence |
| text_report, workplace | Text and site category | Required; legacy TEXT_REPORT/WORKPLACE aliases | Protected | LLM and site thresholds |
| atelectasis, calcification, cardiomegaly, consolidation, fibrosis, mediastinal_widening, nodule, pleural_effusion, pneumoperitoneum, pneumothorax | Scores 0–100 | All ten required; legacy title-case aliases; explicit 0–1 rule supported | Protected | Lunit binarisation |
| tuberculosis | Optional score 0–100 | Allowed, optional; Tuberculosis alias | Protected | Supplemental source compatibility; not an eleventh primary finding |
| patient_age, patient_gender, medical_location_name, ai_priority and source timestamps | Model-compatible demographics/categories/dates | Optional; configured aliases and explicit date/timezone rules | Protected | Existing timing/report semantics |
| report_turn_around_minutes | Virtual nonnegative duration, minutes; REPORT_TURN_AROUND_TIME alias | Optional; removed before ORM write | Protected | Clinical-decision duration converts to seconds |
| study_id_anonymized, study_id, patient_name, patient_id, study_description, feedback, comments, status | Independent source annotations | Optional | Fill only empty values; preserve zero/False and report disagreements | No automatic rerun |
| Other physical source fields | Fixed model storage type | Optional, validated through catalogue | Protected | Legacy source preservation |
| llm_grade, gt_llm, lunit_binarised | Generated grade 1–6 and binary outcomes | Upload forbidden | Forbidden | Strict required LLM success; prompt contract is 1–6 despite stale legacy 1–5 labels |
| ten *_llm fields, tb_llm | Generated binary findings | Upload forbidden | Forbidden | Required only when supplemental processing requested |
| time_end_to_end_seconds, time_to_clinical_decision_seconds | Generated durations, seconds | Upload forbidden | Forbidden | Optional source timing; malformed supplied timing rejects |
| gt_manual | Dedicated manual GT workflow | Generic upload forbidden | Forbidden | Existing manual GT routes remain |
| created_at, updated_at, processing_batch_id | Generated metadata | Upload forbidden | Forbidden | Persistence |

One canonical row per identifier after cross-file merging. Complete new rows alone process. Incomplete new rows are discarded visibly; malformed supplied values still reject the entire submission. Existing studies never automatically reprocess. Required output cardinality, identity, source preservation and schema validate before an atomic insert/enrichment transaction. Saved report cards snapshot referenced field semantics; legacy cards retain legacy definitions.
