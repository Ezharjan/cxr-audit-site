# PRIMER — LLM-based Chest X-Ray Audit Tool
# Copyright (C) 2026 Goh Shu Wen
# Licensed under AGPL-3.0-or-later. See LICENSE at the repository root.
from lib_audit_cxr_v2 import CXRClassifier
import pandas as pd
import numpy as np
import os
import re
import json
import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from typing import Dict, List, Tuple, Optional, Callable
import time
from tqdm import tqdm


# Type alias for progress callback function
# Signature: callback(step_name: str, current: int, total: int, message: str)
ProgressCallback = Callable[[str, int, int, str], None]

def clean_text(text):
    """
    Cleans the input text by removing unwanted characters and formatting.
    """
    # Remove the header
    text = re.sub(r'CHEST\sof\s\d+-\S{3}-\d{4}:', '', text)
    text = re.sub(r'Accession\sNo:\S+', '', text)
    # Replace multiple newlines with a single newline
    text = re.sub(r'\n+', '\n', text)
    # Remove leading and trailing spaces
    text = text.strip()
    return text

def extract_grade(result):
    """Helper function to safely extract grade from result"""
    if isinstance(result, dict):
        return result.get('grade', 0)
    return 0

def extract_hybrid_result(result):
    """Helper function to safely extract hybrid grading result"""
    if isinstance(result, dict):
        return result.get('grade', 0), result.get('explanation', '')
    return 0, ''

class BatchCXRProcessor:
    """
    Concurrent batch processor for CXR reports using CXRClassifier.
    
    This class provides methods to process multiple CXR reports concurrently,
    significantly speeding up the I/O limited LLM processing steps.
    """
    
    def __init__(self, 
                 findings_dict: Dict, 
                 tubes_lines_dict: Dict, 
                 diagnoses_dict: Dict,
                 model_name: str = "gpt-4o-mini",
                 base_url: Optional[str] = None,
                 api_key: Optional[str] = None,
                 max_workers: int = 5,
                 rate_limit_delay: float = 0.1,
                 progress_callback: Optional[ProgressCallback] = None):
        """
        Initialize the batch processor.
        
        Args:
            findings_dict: Dictionary of medical findings
            tubes_lines_dict: Dictionary of tubes and lines
            diagnoses_dict: Dictionary of diagnoses
            model_name: Name of the model to use
            base_url: Base URL for the API (for Ollama use: "http://localhost:11434/v1")
            api_key: API key (use "dummy" for Ollama)
            max_workers: Maximum number of concurrent workers
            rate_limit_delay: Delay between requests to avoid rate limiting
            progress_callback: Optional callback function for progress updates
        """
        self.findings_dict = findings_dict
        self.tubes_lines_dict = tubes_lines_dict
        self.diagnoses_dict = diagnoses_dict
        self.model_name = model_name
        self.base_url = base_url
        self.api_key = api_key
        self.max_workers = max_workers
        self.rate_limit_delay = rate_limit_delay
        self.progress_callback = progress_callback
        
        # Create a classifier instance for each thread
        self._classifiers = {}
        self._lock = threading.Lock()
    
    def set_progress_callback(self, callback: Optional[ProgressCallback]) -> None:
        """Set or update the progress callback function."""
        self.progress_callback = callback
    
    def _update_progress(self, step_name: str, current: int, total: int, message: str = "") -> None:
        """Update progress via callback if available."""
        if self.progress_callback:
            try:
                self.progress_callback(step_name, current, total, message)
            except Exception as e:
                print(f"Error in progress callback: {e}")
    
    def _get_classifier(self) -> CXRClassifier:
        """Get a thread-local CXRClassifier instance."""
        thread_id = threading.get_ident()
        
        if thread_id not in self._classifiers:
            with self._lock:
                if thread_id not in self._classifiers:
                    self._classifiers[thread_id] = CXRClassifier(
                        findings=self.findings_dict,
                        tubes_lines=self.tubes_lines_dict,
                        diagnoses=self.diagnoses_dict,
                        model_name=self.model_name,
                        base_url=self.base_url,
                        api_key=self.api_key
                    )
        
        return self._classifiers[thread_id]
    
    def _process_single_report_semialgo(self, report_text: str, index: int) -> Tuple[int, Dict]:
        """Process a single report using semi-algorithmic method."""
        try:
            time.sleep(self.rate_limit_delay)  # Rate limiting
            classifier = self._get_classifier()
            result = classifier.gradeReportSemialgo(report_text)
            return index, result
        except Exception as e:
            print("Report processing failed")
            return index, {}
    
    def _process_single_report_llm(self, report_text: str, index: int) -> Tuple[int, Dict]:
        """Process a single report using LLM method."""
        try:
            time.sleep(self.rate_limit_delay)  # Rate limiting
            classifier = self._get_classifier()
            result = classifier.gradeReportLLM(report_text)
            return index, result
        except Exception as e:
            print("Report processing failed")
            return index, {}
    
    def _process_single_report_hybrid(self, report_text: str, grade_semialgo: int, index: int) -> Tuple[int, Dict]:
        """Process a single report using hybrid method."""
        try:
            time.sleep(self.rate_limit_delay)  # Rate limiting
            classifier = self._get_classifier()
            result = classifier.gradeReportHybrid(report_text, grade_semialgo)
            return index, result
        except Exception as e:
            print("Report processing failed")
            return index, {}
    
    def _process_single_report_judge(self, report_text: str, algo_grade: int, llm_grade: int, index: int, manual_grade: Optional[int] = None) -> Tuple[int, Dict]:
        """Process a single report using judge method."""
        try:
            time.sleep(self.rate_limit_delay)  # Rate limiting
            classifier = self._get_classifier()

            # Call gradeReportJudge which returns a pandas Series
            result_dict = classifier.gradeReportJudge(report_text, algo_grade, llm_grade, manual_grade)

            return index, result_dict
        except Exception as e:
            print("Report processing failed")
            # Return default values matching the expected output
            return index, {
                'judge_grade': 0,
                'judge_choice': 0, 
                'judge_reasoning': '',
                'judge_grade_ext': 0,
                'judge_choice_ext': 0,
                'judge_reasoning_ext': ''
            }
    
    def _process_single_report_lunit(self, report_text: str, index: int) -> Tuple[int, Dict]:
        """Process a single report using Lunit extraction method."""
        try:
            time.sleep(self.rate_limit_delay)  # Rate limiting
            classifier = self._get_classifier()
            result = classifier.gradeLunit(report_text)
            return index, result
        except Exception as e:
            print("Report processing failed")
            return index, {}
    
    def process_semialgo_batch(self, df: pd.DataFrame, report_column: str = 'REPORT') -> pd.DataFrame:
        """
        Process a batch of reports using semi-algorithmic method concurrently.
        
        Args:
            df: DataFrame containing reports
            report_column: Name of the column containing report text
            
        Returns:
            DataFrame with added semi-algorithmic grading columns
        """
        print(f"Processing {len(df)} reports with semi-algorithmic method using {self.max_workers} workers...")
        
        df_result = df.copy()
        results = {}
        
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Submit all tasks
            future_to_index = {
                executor.submit(self._process_single_report_semialgo, row[report_column], idx): idx 
                for idx, row in df.iterrows()
            }
            
            # Collect results with progress bar
            for future in tqdm(as_completed(future_to_index), total=len(future_to_index), desc="Semi-algo processing"):
                try:
                    index, result = future.result()
                    results[index] = result
                except Exception as e:
                    index = future_to_index[future]
                    print("Report processing failed")
                    results[index] = {}
        
        # Add results to dataframe
        semialgo_data = []
        for idx in df.index:
            if idx in results:
                semialgo_data.append(results[idx])
            else:
                semialgo_data.append({})
        
        semialgo_df = pd.DataFrame(semialgo_data, index=df.index)
        return pd.concat([df_result, semialgo_df], axis=1)
    
    def process_llm_batch(self, df: pd.DataFrame, report_column: str = 'REPORT', strict=False) -> pd.DataFrame:
        """
        Process a batch of reports using LLM method concurrently.
        
        Args:
            df: DataFrame containing reports
            report_column: Name of the column containing report text
            
        Returns:
            DataFrame with added LLM grading column
        """
        print(f"Processing {len(df)} reports with LLM method using {self.max_workers} workers...")
        
        df_result = df.copy()
        results = {}
        total = len(df)
        completed = 0
        
        self._update_progress("llm", 0, total, f"Starting LLM grading for {total} reports...")
        
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Submit all tasks
            future_to_index = {
                executor.submit(self._process_single_report_llm, row[report_column], idx): idx 
                for idx, row in df.iterrows()
            }
            
            # Collect results with progress bar
            for future in tqdm(as_completed(future_to_index), total=len(future_to_index), desc="LLM processing"):
                try:
                    index, result = future.result()
                    grade = extract_grade(result)
                    if strict and (type(grade) is not int or not 1 <= grade <= 6):
                        raise ValueError('required_llm_grade_invalid')
                    results[index] = grade
                    completed += 1
                    # Update progress every 5 reports or at completion
                    if completed % 5 == 0 or completed == total:
                        self._update_progress("llm", completed, total, f"LLM grading: {completed}/{total} reports processed")
                except Exception as e:
                    if strict:
                        raise ValueError('required_llm_step_failed') from None
                    index = future_to_index[future]
                    print("Report processing failed")
                    results[index] = 0
                    completed += 1
        
        self._update_progress("llm", total, total, "LLM grading complete")
        
        # Add results to dataframe
        df_result['llm_grade'] = [results.get(idx, 0) for idx in df.index]
        return df_result
    
    def process_hybrid_batch(self, df: pd.DataFrame, report_column: str = 'REPORT', 
                           semialgo_column: str = 'overall_max_priority') -> pd.DataFrame:
        """
        Process a batch of reports using hybrid method concurrently.
        
        Args:
            df: DataFrame containing reports and semi-algo grades
            report_column: Name of the column containing report text
            semialgo_column: Name of the column containing semi-algo grades
            
        Returns:
            DataFrame with added hybrid grading columns
        """
        print(f"Processing {len(df)} reports with hybrid method using {self.max_workers} workers...")
        
        df_result = df.copy()
        results = {}
        
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Submit all tasks
            future_to_index = {
                executor.submit(self._process_single_report_hybrid, 
                               row[report_column], row[semialgo_column], idx): idx 
                for idx, row in df.iterrows()
            }
            
            # Collect results with progress bar
            for future in tqdm(as_completed(future_to_index), total=len(future_to_index), desc="Hybrid processing"):
                try:
                    index, result = future.result()
                    grade, explanation = extract_hybrid_result(result)
                    results[index] = {'grade': grade, 'explanation': explanation}
                except Exception as e:
                    index = future_to_index[future]
                    print("Report processing failed")
                    results[index] = {'grade': 0, 'explanation': ''}
        
        # Add results to dataframe
        df_result['hybrid_grade'] = [results.get(idx, {}).get('grade', 0) for idx in df.index]
        df_result['hybrid_explanation'] = [results.get(idx, {}).get('explanation', '') for idx in df.index]
        return df_result
    
    def process_judge_batch(self, df: pd.DataFrame, report_column: str = 'REPORT',
                          manual_column: Optional[str] = 'priority_manual',
                          algo_column: str = 'priority_algo', 
                          llm_column: str = 'priority_llm') -> pd.DataFrame:
        """
        Process a batch of reports using judge method concurrently.
        
        Args:
            df: DataFrame containing reports and existing grades
            report_column: Name of the column containing report text
            manual_column: Name of the column containing manual grades (optional)
            algo_column: Name of the column containing algorithmic grades
            llm_column: Name of the column containing LLM grades
            
        Returns:
            DataFrame with added judge grading columns
        """
        print(f"Processing {len(df)} reports with judge method using {self.max_workers} workers...")
        
        df_result = df.copy()
        results = {}
        
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Submit all tasks using the unified single report method
            future_to_index = {
                executor.submit(self._process_single_report_judge, 
                               row[report_column], 
                               row[algo_column], row[llm_column], i, 
                               manual_grade=row[manual_column] if manual_column and manual_column in df.columns else None): i 
                for i, (idx, row) in enumerate(df.iterrows())
            }
            
            # Collect results with progress bar
            for future in tqdm(as_completed(future_to_index), total=len(future_to_index), desc="Judge processing"):
                try:
                    index, result = future.result()
                    results[index] = result
                except Exception as e:
                    index = future_to_index[future]
                    print("Report processing failed")
                    results[index] = {
                        'judge_grade': 0,
                        'judge_choice': 0,
                        'judge_reasoning': '',
                        'judge_grade_ext': 0,
                        'judge_choice_ext': 0,
                        'judge_reasoning_ext': ''
                    }
        
        # Add results to dataframe - now using the consistent approach
        for column in ['judge_grade', 'judge_choice', 'judge_reasoning', 
                      'judge_grade_ext', 'judge_choice_ext', 'judge_reasoning_ext']:
            df_result[column] = [results.get(idx, {}).get(column, 0 if 'grade' in column or 'choice' in column else '') 
                                for idx in df.index]
        
        return df_result
    
    def process_lunit_batch(self, df: pd.DataFrame, report_column: str = 'REPORT', strict=False) -> pd.DataFrame:
        """
        Process a batch of reports using Lunit extraction method concurrently.
        
        Args:
            df: DataFrame containing reports
            report_column: Name of the column containing report text
            
        Returns:
            DataFrame with added Lunit findings column
        """
        print(f"Processing {len(df)} reports with Lunit extraction method using {self.max_workers} workers...")
        
        df_result = df.copy()
        results = {}
        total = len(df)
        completed = 0
        
        self._update_progress("lunit", 0, total, f"Starting detailed findings extraction for {total} reports...")
        
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Submit all tasks
            future_to_index = {
                executor.submit(self._process_single_report_lunit, row[report_column], idx): idx 
                for idx, row in df.iterrows()
            }
            
            # Collect results with progress bar
            for future in tqdm(as_completed(future_to_index), total=len(future_to_index), desc="Supplemental processing"):
                try:
                    index, result = future.result()
                    if strict and (not isinstance(result, dict) or set(result) != {'atelectasis_llm', 'calcification_llm', 'cardiomegaly_llm', 'consolidation_llm', 'fibrosis_llm', 'mediastinal_widening_llm', 'nodule_llm', 'pleural_effusion_llm', 'pneumoperitoneum_llm', 'pneumothorax_llm', 'tb'} or any(type(v) is not bool for v in result.values())):
                        raise ValueError('required_findings_invalid')
                    results[index] = result
                    completed += 1
                    # Update progress every 5 reports or at completion
                    if completed % 5 == 0 or completed == total:
                        self._update_progress("lunit", completed, total, f"Detailed findings: {completed}/{total} reports processed")
                except Exception as e:
                    if strict:
                        raise ValueError('required_supplemental_step_failed') from None
                    index = future_to_index[future]
                    print("Report processing failed")
                    results[index] = {'atelectasis_llm': False, 'calcification_llm': False, 'cardiomegaly_llm': False, 'consolidation_llm': False, 
                                      'fibrosis_llm': False, 'mediastinal_widening_llm': False, 'nodule_llm': False, 'pleural_effusion_llm': False, 
                                      'pneumoperitoneum_llm': False, 'pneumothorax_llm': False, 'tb': False}
                    completed += 1
        
        self._update_progress("lunit", total, total, "Detailed findings extraction complete")
        
        # Add results to dataframe - columns = 'atelectasis_llm', 'tb'
        for column in ['atelectasis_llm', 'calcification_llm', 'cardiomegaly_llm', 'consolidation_llm', 'fibrosis_llm', 'mediastinal_widening_llm', 
                       'nodule_llm', 'pleural_effusion_llm', 'pneumoperitoneum_llm', 'pneumothorax_llm', 'tb']:
            df_result[column] = [results.get(idx, {}).get(column, '') for idx in df.index]
            
        return df_result
    
    def process_full_pipeline(self, df: pd.DataFrame, report_column: str = 'REPORT',
                            steps: List[str] = ['semialgo', 'hybrid', 'llm', 'judge','lunit'],
                            save_intermediate: bool = True, gt_present: bool = False, output_dir: Optional[str] = None, strict=False) -> pd.DataFrame:
        """
        Process the full pipeline: semialgo -> hybrid -> LLM -> judge -> lunit
        
        Args:
            df: DataFrame containing reports
            report_column: Name of the column containing report text
            steps: List of processing steps to apply (can include: 'semialgo', 'hybrid', 'llm', 'judge', 'lunit')
            save_intermediate: Whether to save intermediate results
            gt_present: Whether ground truth data is present
            output_dir: Directory to save intermediate files
            
        Returns:
            DataFrame with all grading columns added
        """
        print("Starting full CXR processing pipeline...")
        if output_dir is None:
            # Platform temp dir: /tmp on Linux containers, %TEMP% on Windows.
            import tempfile
            output_dir = tempfile.gettempdir()
        # Validate steps parameter
        valid_steps = {'semialgo', 'hybrid', 'llm', 'judge', 'lunit'}
        invalid_steps = set(steps) - valid_steps
        if invalid_steps:
            raise ValueError(f"Invalid steps: {invalid_steps}. Valid steps are: {valid_steps}")
        
        start_time = time.time()
        
        # Clean reports
        df_processed = df.copy()
        df_processed[report_column] = df_processed[report_column].apply(clean_text)
        
        # Step 1: Semi-algorithmic processing
        if 'semialgo' in steps:
            print("\n=== Step 1: Semi-algorithmic Processing ===")
            df_processed = self.process_semialgo_batch(df_processed, report_column)
            if save_intermediate:
                df_processed.to_csv(os.path.join(output_dir, 'intermediate_semialgo.csv'), index=False)
        
        # Step 2: Hybrid processing
        if 'hybrid' in steps:
            print("\n=== Step 2: Hybrid Processing ===")
            df_processed = self.process_hybrid_batch(df_processed, report_column)
            if save_intermediate:
                df_processed.to_csv(os.path.join(output_dir, 'intermediate_hybrid.csv'), index=False)
        
        # Step 3: LLM processing
        if 'llm' in steps:
            print("\n=== Step 3: LLM Processing ===")
            df_processed = self.process_llm_batch(df_processed, report_column, strict=strict)
            if save_intermediate:
                df_processed.to_csv(os.path.join(output_dir, 'intermediate_llm.csv'), index=False)
        
        # Step 4: Prepare for judge processing (rename columns if needed)
        if 'judge' in steps:
            if 'priority_manual' not in df_processed.columns and 'GROUND_TRUTH' in df_processed.columns and gt_present:
                df_processed = df_processed.rename(columns={'GROUND_TRUTH': 'priority_manual'})
            if 'priority_hybrid' not in df_processed.columns and 'hybrid_grade' in df_processed.columns:
                df_processed = df_processed.rename(columns={'hybrid_grade': 'priority_hybrid'})
            if 'priority_algo' not in df_processed.columns and 'overall_max_priority' in df_processed.columns:
                df_processed = df_processed.rename(columns={'overall_max_priority': 'priority_algo'})
            if 'priority_llm' not in df_processed.columns and 'llm_grade' in df_processed.columns:
                df_processed = df_processed.rename(columns={'llm_grade': 'priority_llm'})
            
            # Step 5: Judge processing
            print("\n=== Step 4: Judge Processing ===")
            if gt_present:
                if all(col in df_processed.columns for col in ['priority_manual', 'priority_algo', 'priority_llm']):
                    df_processed = self.process_judge_batch(df_processed, report_column)
                else:
                    print("Skipping judge processing - missing required columns")
            else:
                if all(col in df_processed.columns for col in ['priority_algo', 'priority_llm']):
                    df_processed = self.process_judge_batch(df_processed, report_column, manual_column=None)
        
        # Step 5: Lunit extraction (supplemental)
        if 'lunit' in steps:
            print("\n=== Step 5: Lunit Extraction (Supplemental) ===")
            df_processed = self.process_lunit_batch(df_processed, report_column, strict=strict)
            if save_intermediate:
                df_processed.to_csv(os.path.join(output_dir, 'intermediate_lunit.csv'), index=False)
        
        end_time = time.time()
        print(f"\n=== Pipeline Complete ===")
        print(f"Total processing time: {end_time - start_time:.2f} seconds")
        print(f"Average time per report: {(end_time - start_time) / len(df):.2f} seconds")
        
        return df_processed

