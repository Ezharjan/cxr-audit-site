# PRIMER — LLM-based Chest X-Ray Audit Tool
# Copyright (C) 2026 Goh Shu Wen
# Licensed under AGPL-3.0-or-later. See LICENSE at the repository root.
"""
Django models for CXR Analysis data storage.

The primary key is the accession number. Before adding entries, 
the system checks if an accession number already exists.
"""

from django.db import models
from django.utils import timezone


class CXRStudy(models.Model):
    """
    Main model storing processed CXR study data.
    The accession_no is the primary key as specified in requirements.
    """
    
    # Primary key - Accession Number
    accession_no = models.BigIntegerField(primary_key=True, verbose_name="Accession Number")
    
    # Study identifiers
    study_id_anonymized = models.CharField(max_length=255, blank=True, null=True, verbose_name="Anonymized Study ID")
    study_id = models.CharField(max_length=255, blank=True, null=True, verbose_name="Study ID")
    
    # Patient information
    patient_name = models.CharField(max_length=255, blank=True, null=True, verbose_name="Patient Name")
    patient_id = models.CharField(max_length=255, blank=True, null=True, verbose_name="Patient ID")
    patient_age = models.IntegerField(blank=True, null=True, verbose_name="Patient Age")
    patient_gender = models.CharField(max_length=10, blank=True, null=True, verbose_name="Patient Gender")
    
    # Study details
    instances = models.IntegerField(blank=True, null=True, verbose_name="Number of Instances")
    study_description = models.CharField(max_length=500, blank=True, null=True, verbose_name="Study Description")
    upload_date = models.DateTimeField(blank=True, null=True, verbose_name="Upload Date")
    inference_date = models.DateTimeField(blank=True, null=True, verbose_name="Inference Date")
    
    # Location info
    workplace = models.CharField(max_length=50, blank=True, null=True, verbose_name="Workplace Code")
    medical_location_name = models.CharField(max_length=255, blank=True, null=True, verbose_name="Medical Location")
    
    # Lunit AI scores (probability 0-100)
    abnormal = models.FloatField(blank=True, null=True, verbose_name="Abnormal Score")
    atelectasis = models.FloatField(blank=True, null=True, verbose_name="Atelectasis Score")
    calcification = models.FloatField(blank=True, null=True, verbose_name="Calcification Score")
    cardiomegaly = models.FloatField(blank=True, null=True, verbose_name="Cardiomegaly Score")
    consolidation = models.FloatField(blank=True, null=True, verbose_name="Consolidation Score")
    fibrosis = models.FloatField(blank=True, null=True, verbose_name="Fibrosis Score")
    mediastinal_widening = models.FloatField(blank=True, null=True, verbose_name="Mediastinal Widening Score")
    nodule = models.FloatField(blank=True, null=True, verbose_name="Nodule Score")
    pleural_effusion = models.FloatField(blank=True, null=True, verbose_name="Pleural Effusion Score")
    pneumoperitoneum = models.FloatField(blank=True, null=True, verbose_name="Pneumoperitoneum Score")
    pneumothorax = models.FloatField(blank=True, null=True, verbose_name="Pneumothorax Score")
    tuberculosis = models.FloatField(blank=True, null=True, verbose_name="Tuberculosis Score")
    
    # AI workflow info
    ai_report = models.TextField(blank=True, null=True, verbose_name="AI Report")
    ai_priority = models.CharField(max_length=50, blank=True, null=True, verbose_name="AI Priority")
    ai_flag_received_date = models.DateTimeField(blank=True, null=True, verbose_name="AI Flag Received Date")
    feedback = models.TextField(blank=True, null=True, verbose_name="Feedback")
    comments = models.TextField(blank=True, null=True, verbose_name="Comments")
    status = models.CharField(max_length=50, blank=True, null=True, verbose_name="Status")
    
    # Procedure timing
    procedure_start_date = models.DateTimeField(blank=True, null=True, verbose_name="Procedure Start Date")
    procedure_end_date = models.DateTimeField(blank=True, null=True, verbose_name="Procedure End Date")
    
    # Time analysis fields (stored in seconds for precision)
    time_end_to_end_seconds = models.FloatField(blank=True, null=True, verbose_name="End-to-End Time (seconds)")
    time_to_clinical_decision_seconds = models.FloatField(blank=True, null=True, verbose_name="Time to Clinical Decision (seconds)")
    
    # Ground truth / grading
    text_report = models.TextField(blank=True, null=True, verbose_name="Radiologist Text Report (RIS)")
    gt_manual = models.SmallIntegerField(blank=True, null=True, verbose_name="Manual Ground Truth (0/1)")
    gt_llm = models.SmallIntegerField(blank=True, null=True, verbose_name="LLM Ground Truth (0/1)")
    lunit_binarised = models.SmallIntegerField(blank=True, null=True, verbose_name="Lunit Binarised (0/1)")
    llm_grade = models.SmallIntegerField(blank=True, null=True, verbose_name="LLM Grade (1-5)")
    
    # Supplemental LLM findings (if detailed extraction was run)
    atelectasis_llm = models.SmallIntegerField(blank=True, null=True, verbose_name="Atelectasis LLM")
    calcification_llm = models.SmallIntegerField(blank=True, null=True, verbose_name="Calcification LLM")
    cardiomegaly_llm = models.SmallIntegerField(blank=True, null=True, verbose_name="Cardiomegaly LLM")
    consolidation_llm = models.SmallIntegerField(blank=True, null=True, verbose_name="Consolidation LLM")
    fibrosis_llm = models.SmallIntegerField(blank=True, null=True, verbose_name="Fibrosis LLM")
    mediastinal_widening_llm = models.SmallIntegerField(blank=True, null=True, verbose_name="Mediastinal Widening LLM")
    nodule_llm = models.SmallIntegerField(blank=True, null=True, verbose_name="Nodule LLM")
    pleural_effusion_llm = models.SmallIntegerField(blank=True, null=True, verbose_name="Pleural Effusion LLM")
    pneumoperitoneum_llm = models.SmallIntegerField(blank=True, null=True, verbose_name="Pneumoperitoneum LLM")
    pneumothorax_llm = models.SmallIntegerField(blank=True, null=True, verbose_name="Pneumothorax LLM")
    tb_llm = models.SmallIntegerField(blank=True, null=True, verbose_name="TB LLM")
    
    # Metadata
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Record Created At")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Record Updated At")
    processing_batch_id = models.CharField(max_length=36, blank=True, null=True, verbose_name="Processing Batch ID")
    
    class Meta:
        verbose_name = "CXR Study"
        verbose_name_plural = "CXR Studies"
        ordering = ['-procedure_start_date']
        indexes = [
            models.Index(fields=['workplace']),
            models.Index(fields=['procedure_start_date']),
            models.Index(fields=['gt_llm']),
            models.Index(fields=['lunit_binarised']),
            models.Index(fields=['processing_batch_id']),
        ]
    
    def __str__(self):
        return f"CXR Study {self.accession_no}"
    
    @classmethod
    def accession_exists(cls, accession_no):
        """Check if an accession number already exists in the database."""
        return cls.objects.filter(accession_no=accession_no).exists()
    
    @classmethod
    def get_or_none(cls, accession_no):
        """Get study by accession number or return None."""
        try:
            return cls.objects.get(accession_no=accession_no)
        except cls.DoesNotExist:
            return None


class ProcessingTask(models.Model):
    """
    Model to track background processing tasks for file uploads.
    """
    
    STATUS_CHOICES = [
        ('queued', 'Queued'),
        ('processing', 'Processing'),
        ('saving', 'Saving results'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ]
    
    task_id = models.CharField(max_length=36, primary_key=True, verbose_name="Task ID")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='queued', verbose_name="Status")
    
    # Progress tracking
    progress_message = models.CharField(max_length=500, blank=True, null=True, verbose_name="Progress Message")
    progress_percent = models.FloatField(default=0, verbose_name="Progress Percentage")
    progress_current = models.IntegerField(default=0, verbose_name="Current Progress")
    progress_total = models.IntegerField(default=0, verbose_name="Total Items")
    progress_step = models.CharField(max_length=50, blank=True, null=True, verbose_name="Current Step")
    
    # Results
    txt_report = models.TextField(blank=True, null=True, verbose_name="Text Report")
    csv_data = models.TextField(blank=True, null=True, verbose_name="CSV Data")
    error_message = models.TextField(blank=True, null=True, verbose_name="Error Message")
    
    results_saved_at = models.DateTimeField(null=True, blank=True)

    # Statistics
    total_records_processed = models.IntegerField(default=0, verbose_name="Total Records Processed")
    new_records_added = models.IntegerField(default=0, verbose_name="New Records Added")
    existing_records_skipped = models.IntegerField(default=0, verbose_name="Existing Records Skipped")
    
    # False negatives data (stored as JSON)
    false_negatives_json = models.TextField(blank=True, null=True, verbose_name="False Negatives JSON")
    
    # Timing
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Created At")
    completed_at = models.DateTimeField(blank=True, null=True, verbose_name="Completed At")
    
    # Configuration
    supplemental_steps = models.BooleanField(default=False, verbose_name="Supplemental Steps Enabled")

    TASK_TYPE_CHOICES = [
        ('upload', 'Upload'),
        ('backfill', 'Backfill'),
    ]
    task_type = models.CharField(max_length=20, choices=TASK_TYPE_CHOICES, default='upload', verbose_name="Task Type")
    
    class Meta:
        verbose_name = "Processing Task"
        verbose_name_plural = "Processing Tasks"
        ordering = ['-created_at']
    
    def __str__(self):
        return f"Task {self.task_id} ({self.status})"
    
    def mark_completed(self):
        """Mark task as completed."""
        self.status = 'completed'
        self.completed_at = timezone.now()
        self.progress_percent = 100
        self.save()
    
    def mark_failed(self, error_message):
        """Mark task as failed with error message."""
        self.status = 'failed'
        self.completed_at = timezone.now()
        self.error_message = error_message
        self.save()
    
    def update_progress(self, step: str, current: int, total: int, message: str, percent: float = None):
        """Update progress information."""
        self.progress_step = step
        self.progress_current = current
        self.progress_total = total
        self.progress_message = message
        if percent is not None:
            self.progress_percent = percent
        self.save(update_fields=['progress_step', 'progress_current', 'progress_total', 
                                  'progress_message', 'progress_percent'])


class UploadedFile(models.Model):
    """
    Model to track uploaded files associated with processing tasks.
    """
    
    FILE_TYPE_CHOICES = [
        ('carpl', 'CARPL/Lunit File'),
        ('ge', 'GE/RIS File'),
    ]
    
    task = models.ForeignKey(ProcessingTask, on_delete=models.CASCADE, related_name='files')
    file_type = models.CharField(max_length=10, choices=FILE_TYPE_CHOICES, verbose_name="File Type")
    original_filename = models.CharField(max_length=255, verbose_name="Original Filename")
    file_path = models.CharField(max_length=500, verbose_name="Stored File Path")
    file_size = models.BigIntegerField(verbose_name="File Size (bytes)")
    uploaded_at = models.DateTimeField(auto_now_add=True, verbose_name="Uploaded At")
    
    class Meta:
        verbose_name = "Uploaded File"
        verbose_name_plural = "Uploaded Files"
    
    def __str__(self):
        return f"{self.original_filename} ({self.file_type})"


class SubmissionReservation(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    token = models.UUIDField(null=True)
    task = models.ForeignKey(ProcessingTask, null=True, on_delete=models.SET_NULL)
    acquired_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name="upload_submission_singleton")]
