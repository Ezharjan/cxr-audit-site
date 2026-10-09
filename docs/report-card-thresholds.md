# Configurable Lunit thresholds

In the report layout editor, edit a table or graph and open **Optional settings**.
Choose **Lunit findings (policy-derived)** as the prediction source and a threshold policy.
The **Lunit thresholds** controls let you include findings, set a default cutoff, and override individual cutoffs.
Apply updates the draft; publish through the existing report workflow to make it available to report users.
Reset to policy defaults removes the custom settings for that card.

For an all-findings chart at >10, include every finding and enter default cutoff 10.
For pneumothorax at >95, include only pneumothorax, enter default cutoff 95, and choose **LLM pneumothorax** as the reference.
The all-findings chart is a combined any-positive result; separate finding analyses use separate cards.

The optional configuration is stored with each card's query:

```yaml
query:
  measurement: accuracy
  inputs:
    ground_truth: pneumothorax_llm
    prediction: lunit_findings
  threshold_policy: lunit-defaults@1
  lunit_thresholds:
    findings: [pneumothorax]
    default: 95
```

Omit `findings` to include all policy findings. Omit `default` to use each finding's policy cutoff.
An `overrides` mapping wins over the default, for example `overrides: {pneumothorax: 95}` with a default of 10.
Only included findings may have overrides. Cutoffs must be finite numbers on the 0–100 score scale.
The comparison is strictly greater-than: a score equal to the cutoff is negative.
Missing required selected scores exclude a study from that card's measurement; unselected missing scores do not.

Original Lunit scores, stored binary labels and LLM classifications are never rewritten.
Tables, charts, CSV, frozen print/PDF and email use the shared evaluation path and include effective cutoffs.
Published report versions retain the settings selected at publication.

Thresholding uses inexpensive numeric comparisons and does not call the LLM.
The existing protected table paging cache also includes the card definition in its identity.
No new persistent metric cache, dependency or database migration is needed for this upgrade.
