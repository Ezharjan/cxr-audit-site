#!/usr/bin/env python3
# PRIMER — LLM-based Chest X-Ray Audit Tool
# Copyright (C) 2026 Goh Shu Wen
# Licensed under AGPL-3.0-or-later. See LICENSE at the repository root.
"""
Combined CXR Analysis Server
Runs both the API server (port 1221) and static file server (port 1220) in a single Python file.
"""

from fastapi import FastAPI, File, UploadFile, HTTPException, BackgroundTasks, Depends, Request, Security
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
import pandas as pd
import numpy as np
import json
import tempfile
import os
import uuid
from typing import Optional, Dict, Any, List
from pydantic import BaseModel
import asyncio
from datetime import datetime
import uvicorn
import argparse
import threading
import time
import requests

# Import your existing class
from class_process_carpl import ProcessCarpl, processor as _batch_processor
from open_protected_xlsx import open_protected_xlsx

thresholds = {
    'default': {
        'Atelectasis': 10,
        'Calcification': 10,
        'Cardiomegaly': 10,
        'Consolidation': 10,
        'Fibrosis': 10,
        'Mediastinal Widening': 10,
        'Nodule': 15,
        'Pleural Effusion': 10,
        'Pneumoperitoneum': 10,
        'Pneumothorax': 10,
        'Tuberculosis': 999
    },
    'YIS': {
        'Nodule': 5
    }
}

# ================================
# API KEY AUTHENTICATION
# ================================

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def _env_flag(name: str, default: str = "False") -> bool:
    return os.environ.get(name, default).lower() in ("true", "1", "yes")


def api_auth_required() -> bool:
    """Return whether service API authentication is required for this process."""
    return _env_flag("API_REQUIRE_AUTH") or not _env_flag("DJANGO_DEBUG", "False")


def configured_api_secret() -> str:
    return os.environ.get("API_SECRET_KEY", "")


def configured_cors_origins() -> List[str]:
    raw_origins = os.environ.get("FASTAPI_CORS_ORIGINS", "")
    if raw_origins:
        return [origin.strip() for origin in raw_origins.split(",") if origin.strip()]
    if api_auth_required():
        return []
    return ["*"]


def local_dev_mode() -> bool:
    """Explicit single-workstation profile (PRIMER_LOCAL_DEV); never implicit."""
    return _env_flag("PRIMER_LOCAL_DEV")


_PLACEHOLDER_SECRETS = {"", "change-me", "change-me-to-a-random-secret", "changeme", "dummy"}


def _is_loopback(host: str) -> bool:
    import ipaddress
    host = (host or "").strip().strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def local_dev_startup_errors(bind_host: str) -> List[str]:
    """Reasons the local development profile must refuse to start (no secret values)."""
    if not local_dev_mode():
        return []
    errors = []
    if not _is_loopback(bind_host):
        errors.append("PRIMER_LOCAL_DEV requires a loopback bind host (127.0.0.1)")
    if not _env_flag("API_REQUIRE_AUTH"):
        errors.append("PRIMER_LOCAL_DEV requires API_REQUIRE_AUTH=True")
    secret = configured_api_secret()
    if secret.lower() in _PLACEHOLDER_SECRETS or len(secret) < 32:
        errors.append("PRIMER_LOCAL_DEV requires a generated API_SECRET_KEY (32+ characters)")
    from urllib.parse import urlparse
    try:
        llm_host = urlparse(os.environ.get("OLLAMA_BASE_URL", "")).hostname
    except ValueError:
        llm_host = None
    if not _is_loopback(llm_host):
        errors.append("PRIMER_LOCAL_DEV requires a loopback OLLAMA_BASE_URL")
    return errors


async def verify_api_key(request: Request, api_key: str = Security(_api_key_header)):
    """Validate the X-API-Key header against the configured secret."""
    if request.url.path == "/health":
        return
    secret = configured_api_secret()
    if not api_auth_required() and not secret:
        return  # Local development mode without a configured key.
    if not secret:
        raise HTTPException(status_code=503, detail="API authentication is required but API_SECRET_KEY is not configured")
    import hmac
    if not api_key or not hmac.compare_digest(api_key.encode(), secret.encode()):
        raise HTTPException(status_code=403, detail="Invalid or missing API key")

# ================================
# API SERVER (Port 1221)
# ================================

api_app = FastAPI(
    title="CXR Analysis API",
    description="API for processing chest X-ray reports using Lunit and ground truth data",
    version="1.0.0",
    dependencies=[Depends(verify_api_key)],
)

# Add CORS middleware for web frontend access
api_app.add_middleware(
    CORSMiddleware,
    allow_origins=configured_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global storage for processing results (use Redis/database in production)
processing_results = {}

# Response models
class ProcessingResponse(BaseModel):
    task_id: str
    status: str
    message: str

class ProcessingStatus(BaseModel):
    task_id: str
    status: str
    progress: Optional[str] = None
    progress_percent: Optional[float] = None
    progress_details: Optional[Dict[str, Any]] = None
    total_reports: Optional[int] = None
    results: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: str
    completed_at: Optional[str] = None

class AnalysisResults(BaseModel):
    txt_report: str
    data_output: Any  # This can be more specifically typed if needed
    csv_data: str
    filename: str

class BackfillReportItem(BaseModel):
    accession_no: int
    text_report: str

class BackfillRequest(BaseModel):
    reports: List[BackfillReportItem]

def save_uploaded_file(upload_file: UploadFile) -> str:
    """Save uploaded file to temporary location and return path"""
    try:
        # Create temporary file with proper extension
        if not upload_file.filename:
            raise HTTPException(status_code=400, detail="File must have a filename")
        suffix = os.path.splitext(upload_file.filename)[1]
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp_file:
            content = upload_file.file.read()
            tmp_file.write(content)
            tmp_file_path = tmp_file.name
        
        upload_file.file.seek(0)  # Reset file pointer
        return tmp_file_path
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error saving file: {str(e)}")

async def sort_files_async(file_paths: List[str]) -> Dict[str, List[str]]:
    """Sort uploaded files into GE (Excel) and CARPL (CSV) categories"""
    ge_file_paths = []
    carpl_file_paths = []
    
    for file_path in file_paths:
        ext = os.path.splitext(file_path)[1].lower()
        if ext in ['.xlsx', '.xls']:
            # Attempt to open with protected Excel handler
            try:
                df = open_protected_xlsx(file_path, password=os.environ.get("XLSX_DECRYPT_PASSWORD", ""))
            except Exception as e:
                try:
                    df = pd.read_excel(file_path)
                except Exception as e2:
                    print(f"Failed to open Excel file {file_path}: {str(e2)}")
                    raise HTTPException(status_code=400, detail=f"Error opening GE file {os.path.basename(file_path)}: {str(e2)}")
            finally:
                # Verify the Excel file has required columns
                if not {'ACCESSION_NO', 'TEXT_REPORT'}.issubset(df.columns):
                    missing_cols = {'ACCESSION_NO', 'TEXT_REPORT'} - set(df.columns)
                    raise HTTPException(status_code=400, detail=f"GE file {os.path.basename(file_path)} is missing required columns: {missing_cols}")
                ge_file_paths.append(file_path)
                print(f"Successfully identified GE file: {os.path.basename(file_path)}")
        elif ext == '.csv':
            try:
                df = pd.read_csv(file_path)
                # Verify the CSV file has required columns
                if not {'ACCESSION_NO', "Atelectasis", "Calcification", "Cardiomegaly", "Consolidation", "Fibrosis", "Mediastinal Widening", \
                            "Nodule", "Pleural Effusion", "Pneumoperitoneum", "Pneumothorax", "Tuberculosis"}.issubset(df.columns):
                    missing_cols = {'ACCESSION_NO', "Atelectasis", "Calcification", "Cardiomegaly", "Consolidation", "Fibrosis", "Mediastinal Widening", \
                                    "Nodule", "Pleural Effusion", "Pneumoperitoneum", "Pneumothorax", "Tuberculosis"} - set(df.columns)
                    raise HTTPException(status_code=400, detail=f"CARPL file {os.path.basename(file_path)} is missing required columns: {missing_cols}")
                carpl_file_paths.append(file_path)
                print(f"Successfully identified CARPL file: {os.path.basename(file_path)}")
            except Exception as e:
                print(f"Failed to open CSV file {file_path}: {str(e)}")
                raise HTTPException(status_code=400, detail=f"Error opening CARPL file {os.path.basename(file_path)}: {str(e)}")
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported file type: {os.path.basename(file_path)}")

    return {
        "ge_file_paths": ge_file_paths,
        "carpl_file_paths": carpl_file_paths
    }


def create_progress_callback(task_id: str, total_reports: int = 0, supplemental_steps: bool = False):
    """
    Create a progress callback function that updates the processing_results dict.
    
    The callback tracks progress across multiple processing steps:
    - llm: LLM grading (main step)
    - lunit: Detailed findings extraction (supplemental, slow)
    """
    # Track current step and overall progress
    state = {
        'current_step': None,
        'total_reports': total_reports,
        'supplemental_steps': supplemental_steps
    }
    
    def callback(step_name: str, current: int, total: int, message: str):
        state['current_step'] = step_name
        state['total_reports'] = total
        
        # Calculate overall progress percentage based on step weights
        # If supplemental_steps is enabled:
        #   - Loading & init: 5%
        #   - LLM grading: 25% (reports are faster)
        #   - Detailed findings: 55% (much slower, ~10s per report)
        #   - Stats & finalization: 15%
        # If supplemental_steps is disabled:
        #   - Loading & init: 10%
        #   - LLM grading: 70%
        #   - Stats & finalization: 20%
        
        if state['supplemental_steps']:
            if step_name == 'llm':
                # LLM step: 5% to 30%
                step_progress = (current / total) if total > 0 else 0
                overall_progress = 5 + (step_progress * 25)
            elif step_name == 'lunit':
                # Lunit/detailed findings step: 30% to 85%
                step_progress = (current / total) if total > 0 else 0
                overall_progress = 30 + (step_progress * 55)
            else:
                overall_progress = 85
        else:
            if step_name == 'llm':
                # LLM step: 10% to 80%
                step_progress = (current / total) if total > 0 else 0
                overall_progress = 10 + (step_progress * 70)
            else:
                overall_progress = 80
        
        # Update the processing results
        processing_results[task_id]["progress"] = message
        processing_results[task_id]["progress_percent"] = round(overall_progress, 1)
        processing_results[task_id]["progress_details"] = {
            "step": step_name,
            "current": current,
            "total": total,
            "step_message": message
        }
    
    return callback


def process_files_sync(task_id: str, carpl_file_paths: List[str], ge_file_paths: List[str], supplemental_steps: bool = False):
    """Process files synchronously in a background thread"""
    try:
        # Update status to processing
        processing_results[task_id]["status"] = "processing"
        processing_results[task_id]["progress"] = "Initializing ProcessCarpl..."
        processing_results[task_id]["progress_percent"] = 2
        
        # Create a progress callback that will update the processing_results
        progress_callback = create_progress_callback(task_id, supplemental_steps=supplemental_steps)
        
        # Initialize processor with multiple file paths and the progress callback
        processor = ProcessCarpl(
            carpl_file_paths, 
            ge_file_paths, 
            supplemental_steps=supplemental_steps, 
            priority_threshold=thresholds,
            progress_callback=progress_callback
        )
        
        print("Starting task:", task_id)

        # Run the pipeline
        # For this API, we'll run the pipeline manually as we want to collate the text
        df_merged = processor.load_reports()
        
        # Update progress callback with actual report count
        processing_results[task_id]["total_reports"] = len(df_merged)
        
        # Estimate processing time based on whether supplemental steps are enabled
        if supplemental_steps:
            total_seconds = len(df_merged) * 10  # 10 seconds per entry with supplemental steps
        else:
            total_seconds = len(df_merged) / 2  # 2 reports per second without supplemental steps
        minutes = int(total_seconds // 60)
        seconds = int(total_seconds % 60)
        
        processing_results[task_id]["progress"] = f"Loaded {len(df_merged)} reports. Estimated time: {minutes}m {seconds}s.{' (Detailed findings enabled)' if supplemental_steps else ''}"
        processing_results[task_id]["progress_percent"] = 5 if supplemental_steps else 10
        initial_metrics = processor.txt_initial_metrics(df_merged)
        
        df_merged = processor.process_stats_accuracy(df_merged, supplemental_steps=supplemental_steps)
        
        processing_results[task_id]["progress"] = "Calculating accuracy statistics..."
        processing_results[task_id]["progress_percent"] = 88 if supplemental_steps else 82
        stats_accuracy = processor.txt_stats_accuracy(df_merged)

        df_merged = processor.process_stats_time(df_merged)
        processing_results[task_id]["progress"] = "Calculating time statistics..."
        processing_results[task_id]["progress_percent"] = 92 if supplemental_steps else 88
        stats_time = processor.txt_stats_time(df_merged)

        # We disable this for now because it messes with the workflow
        #processor.box_time(df_merged)
        
        # Identify false negatives before rearranging columns
        false_negative_df, false_negative_summary = processor.identify_false_negatives(df_merged)
        processing_results[task_id]["progress"] = "Identified false negative cases"
        processing_results[task_id]["progress_percent"] = 95
        
        # Finally we rearrange the columns to fit the proper format
        df_merged = processor.rearrange_columns(df_merged)
        print("Final output has a shape of:", df_merged.shape)
        df_merged_json = df_merged.to_json(orient='records', indent=4)
        
        # Convert DataFrame to CSV string
        df_merged_csv = df_merged.to_csv(index=False)
        
        # Convert false negative DataFrame to CSV and JSON
        false_negative_csv = false_negative_df.to_csv(index=False) if not false_negative_df.empty else ""
        false_negative_json = false_negative_df.to_json(orient='records', indent=4) if not false_negative_df.empty else "[]"
        
        first_date = df_merged['PROCEDURE_END_DATE'].min()
        last_date = df_merged['PROCEDURE_END_DATE'].max()

        output_json = {
            "txt_report": initial_metrics + "\n" + stats_accuracy + "\n" + stats_time,
            "data_output": json.loads(df_merged_json),
            "filename": f"cxr_analysis_{first_date}_to_{last_date}",
            "csv_data": df_merged_csv,
            "false_negatives": {
                "summary": false_negative_summary,
                "data": json.loads(false_negative_json),
                "csv_data": false_negative_csv
            }
        }

        # Update final status
        processing_results[task_id]["status"] = "completed"
        processing_results[task_id]["results"] = output_json
        processing_results[task_id]["completed_at"] = datetime.now().isoformat()
        processing_results[task_id]["progress"] = "Analysis complete"
        processing_results[task_id]["progress_percent"] = 100
        
        print("Processing complete for task:", task_id)
        
        # Clean up temporary files
        try:
            for file_path in carpl_file_paths + ge_file_paths:
                os.unlink(file_path)
        except:
            pass  # Files might already be deleted
            
    except Exception as e:
        processing_results[task_id]["status"] = "failed"
        processing_results[task_id]["error"] = str(e)
        processing_results[task_id]["completed_at"] = datetime.now().isoformat()

@api_app.post("/analyze", response_model=ProcessingResponse)
async def analyze_files(
    background_tasks: BackgroundTasks,
    lunit_file: UploadFile = File(..., description="Lunit results file (CSV/Excel)"),
    ground_truth_file: UploadFile = File(..., description="Ground truth file (CSV/Excel)"),
    supplemental_steps: bool = False
):
    """
    Upload Lunit and ground truth files for analysis.
    
    - **lunit_file**: CSV or Excel file containing Lunit analysis results
    - **ground_truth_file**: CSV or Excel file containing ground truth data
    - **supplemental_steps**: Enable supplemental processing steps (slower, ~10s per entry)
    """
    
    # Validate file types
    lunit_ext = os.path.splitext(lunit_file.filename or "")[1].lower()
    gt_ext = os.path.splitext(ground_truth_file.filename or "")[1].lower()
    
    allowed_extensions = {'.csv', '.xlsx', '.xls'}
    if lunit_ext not in allowed_extensions or gt_ext not in allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file format. Allowed: {', '.join(allowed_extensions)}"
        )
    
    try:
        # Generate unique task ID
        task_id = str(uuid.uuid4())
        
        # Save uploaded files
        lunit_path = save_uploaded_file(lunit_file)
        gt_path = save_uploaded_file(ground_truth_file)
        
        # Initialize processing status
        processing_results[task_id] = {
            "status": "queued",
            "progress": "Files uploaded, queued for processing",
            "progress_percent": 5,
            "created_at": datetime.now().isoformat(),
            "lunit_filename": lunit_file.filename,
            "gt_filename": ground_truth_file.filename,
            "supplemental_steps": supplemental_steps
        }
        
        # Start background processing
        background_tasks.add_task(
            process_files_sync, 
            task_id, 
            [lunit_path],  # Single file as list
            [gt_path],     # Single file as list
            supplemental_steps
        )
        
        return ProcessingResponse(
            task_id=task_id,
            status="queued",
            message="Files uploaded successfully. Processing started."
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@api_app.post("/analyze-multiple", response_model=ProcessingResponse)
async def analyze_multiple_files(
    background_tasks: BackgroundTasks,
    lunit_files: List[UploadFile] = File(..., description="Multiple Lunit results files (CSV/Excel)"),
    ground_truth_files: List[UploadFile] = File(..., description="Multiple ground truth files (CSV/Excel)"),
    supplemental_steps: bool = False
):
    """
    Upload multiple Lunit and ground truth files for combined analysis.
    
    - **lunit_files**: Multiple CSV or Excel files containing Lunit analysis results
    - **ground_truth_files**: Multiple CSV or Excel files containing ground truth data
    - **supplemental_steps**: Enable supplemental processing steps (slower, ~10s per entry)
    """
    
    # Validate all file types
    allowed_extensions = {'.csv', '.xlsx', '.xls'}
    
    for file in lunit_files + ground_truth_files:
        file_ext = os.path.splitext(file.filename or "")[1].lower()
        if file_ext not in allowed_extensions:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file format for {file.filename}. Allowed: {', '.join(allowed_extensions)}"
            )
    
    try:
        # Generate unique task ID
        task_id = str(uuid.uuid4())
        
        # Save all uploaded files
        lunit_paths = [save_uploaded_file(f) for f in lunit_files]
        gt_paths = [save_uploaded_file(f) for f in ground_truth_files]
        
        # Initialize processing status
        processing_results[task_id] = {
            "status": "queued",
            "progress": f"Files uploaded ({len(lunit_files)} Lunit, {len(ground_truth_files)} GT), queued for processing",
            "progress_percent": 5,
            "created_at": datetime.now().isoformat(),
            "lunit_filenames": [f.filename for f in lunit_files],
            "gt_filenames": [f.filename for f in ground_truth_files],
            "supplemental_steps": supplemental_steps
        }
        
        # Start background processing
        background_tasks.add_task(
            process_files_sync, 
            task_id, 
            lunit_paths,
            gt_paths,
            supplemental_steps
        )
        
        return ProcessingResponse(
            task_id=task_id,
            status="queued",
            message=f"Multiple files uploaded successfully ({len(lunit_files)} Lunit, {len(ground_truth_files)} GT files). Processing started."
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@api_app.post("/analyze-auto-sort", response_model=ProcessingResponse)
async def analyze_auto_sort_files(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(..., description="Mixed files (will be auto-sorted into Lunit/GE categories)"),
    supplemental_steps: bool = False
):
    """
    Upload mixed files and automatically sort them into Lunit (CSV) and GE (Excel) categories for analysis.
    
    - **files**: List of mixed CSV or Excel files (will be automatically categorized)
    - **supplemental_steps**: Enable supplemental processing steps (slower, ~10s per entry)
    """
    
    # Generate unique task ID
    task_id = str(uuid.uuid4())
    
    # Save uploaded files
    try:
        all_file_paths = []
        for file in files:
            if not file.filename:
                raise HTTPException(status_code=400, detail="All files must have filenames")
            file_path = save_uploaded_file(file)
            all_file_paths.append(file_path)
            
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error processing uploaded files: {str(e)}")
    
    # Initialize task status
    processing_results[task_id] = {
        "task_id": task_id,
        "status": "queued",
        "progress": "Files uploaded successfully, sorting files...",
        "progress_percent": 3,
        "results": None,
        "error": None,
        "created_at": datetime.now().isoformat(),
        "completed_at": None,
        "supplemental_steps": supplemental_steps,
        "files": {
            "uploaded_filenames": [f.filename for f in files]
        }
    }
    
    # Sort files and start background processing
    try:
        file_sorting = await sort_files_async(all_file_paths)
        carpl_file_paths = file_sorting["carpl_file_paths"]
        ge_file_paths = file_sorting["ge_file_paths"]
        
        # Update task status with sorting results
        processing_results[task_id]["progress"] = f"Files sorted: {len(carpl_file_paths)} CARPL (CSV), {len(ge_file_paths)} GE (Excel)"
        processing_results[task_id]["progress_percent"] = 5
        processing_results[task_id]["files"]["sorted_files"] = {
            "carpl_count": len(carpl_file_paths),
            "ge_count": len(ge_file_paths)
        }
        
        if not carpl_file_paths:
            raise HTTPException(status_code=400, detail="No CARPL (CSV) files found in uploaded files")
        if not ge_file_paths:
            raise HTTPException(status_code=400, detail="No GE (Excel) files found in uploaded files")
        
        # Start background processing
        background_tasks.add_task(
            process_files_sync, 
            task_id, 
            carpl_file_paths,
            ge_file_paths,
            supplemental_steps
        )
        
    except Exception as e:
        processing_results[task_id]["status"] = "failed"
        processing_results[task_id]["error"] = str(e)
        processing_results[task_id]["completed_at"] = datetime.now().isoformat()
        # Clean up files on error
        try:
            for file_path in all_file_paths:
                os.unlink(file_path)
        except:
            pass
        raise HTTPException(status_code=400, detail=str(e))
    
    return ProcessingResponse(
        task_id=task_id,
        status="queued",
        message=f"Files uploaded and sorted successfully ({len(carpl_file_paths)} CARPL, {len(ge_file_paths)} GE). Processing started in background."
    )



def process_canonical_sync(task_id, rows, supplemental):
    from canonical_processing import process, safe_csv
    from class_process_carpl import processor as batch_processor
    try:
        processing_results[task_id]['status'] = 'processing'
        batch_processor.set_progress_callback(create_progress_callback(task_id, supplemental_steps=supplemental))
        outputs = process(rows, batch_processor, thresholds, supplemental)
        processing_results[task_id].update(status='completed', progress_percent=100,
            progress='Analysis complete', completed_at=datetime.now().isoformat(),
            results={'canonical_rows': outputs, 'txt_report': f'Processed {len(outputs)} studies.', 'csv_data': safe_csv(outputs), 'filename': 'cxr_' + task_id})
    except Exception:
        processing_results[task_id].update(status='failed', error='Required processing failed', completed_at=datetime.now().isoformat())
    finally:
        rows.clear()


@api_app.post('/analyze-canonical', response_model=ProcessingResponse)
async def analyze_canonical(request: Request, background_tasks: BackgroundTasks):
    from canonical_processing import validate_rows
    if request.headers.get('content-type', '').split(';')[0] != 'application/json':
        raise HTTPException(400, detail='JSON required')
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 49 * 1024 * 1024:
            raise HTTPException(413, detail='Payload too large')
        body.extend(chunk)
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict) or set(payload) != {'project', 'rows', 'supplemental_steps'} or payload['project'] != 'prime' or type(payload['supplemental_steps']) is not bool:
            raise ValueError()
        rows = validate_rows(payload['rows'])
    except (ValueError, TypeError, KeyError):
        raise HTTPException(400, detail='Invalid canonical input') from None
    if any(t['status'] in ('queued', 'processing') for t in processing_results.values()):
        raise HTTPException(409, detail='Processor busy')
    task_id = str(uuid.uuid4())
    processing_results[task_id] = dict(task_id=task_id, status='queued', progress='Queued', progress_percent=0,
        results=None, error=None, created_at=datetime.now().isoformat(), completed_at=None,
        supplemental_steps=payload['supplemental_steps'])
    background_tasks.add_task(process_canonical_sync, task_id, rows, payload['supplemental_steps'])
    return ProcessingResponse(task_id=task_id, status='queued', message='Validated studies queued')

@api_app.get("/status/{task_id}", response_model=ProcessingStatus)
async def get_processing_status(task_id: str):
    """Get the current status of a processing task."""
    
    if task_id not in processing_results:
        raise HTTPException(status_code=404, detail="Task not found")
    
    result = processing_results[task_id]
    return ProcessingStatus(
        task_id=task_id,
        status=result["status"],
        progress=result.get("progress"),
        progress_percent=result.get("progress_percent"),
        progress_details=result.get("progress_details"),
        total_reports=result.get("total_reports"),
        results=result.get("results"),
        error=result.get("error"),
        created_at=result["created_at"],
        completed_at=result.get("completed_at")
    )

@api_app.get("/results/{task_id}")
async def get_results(task_id: str):
    """Get the results of a completed processing task."""
    
    if task_id not in processing_results:
        raise HTTPException(status_code=404, detail="Task not found")
    
    result = processing_results[task_id]
    
    if result["status"] != "completed":
        raise HTTPException(status_code=400, detail=f"Task not completed. Current status: {result['status']}")
    
    return result["results"]

@api_app.delete("/tasks/{task_id}")
async def delete_task(task_id: str):
    """Delete a completed task and its results"""
    
    if task_id not in processing_results:
        raise HTTPException(status_code=404, detail="Task not found")
    
    del processing_results[task_id]
    return {"message": "Task deleted successfully"}

@api_app.get("/tasks")
async def list_tasks():
    """List all tasks with their current status"""
    
    task_list = []
    for task_id, task_data in processing_results.items():
        task_list.append({
            "task_id": task_id,
            "status": task_data["status"],
            "created_at": task_data["created_at"],
            "completed_at": task_data.get("completed_at"),
            "files": task_data.get("files", {})
        })
    
    return {"tasks": task_list}

@api_app.get("/")
async def root():
    """API health check and information"""
    return {
        "message": "CXR Analysis API",
        "version": "1.0.0",
        "endpoints": {
            "analyze": "POST /analyze - Upload single files for analysis",
            "analyze-multiple": "POST /analyze-multiple - Upload multiple files for analysis",
            "analyze-auto-sort": "POST /analyze-auto-sort - Upload mixed files (auto-sorted into categories)",
            "status": "GET /status/{task_id} - Check processing status",
            "results": "GET /results/{task_id} - Get analysis results",
            "tasks": "GET /tasks - List all tasks",
            "delete-task": "DELETE /tasks/{task_id} - Delete a task",
            "docs": "GET /docs - Interactive API documentation"
        }
    }

@api_app.get("/health")
async def health():
    """Unauthenticated container health check with no PHI-bearing payload."""
    return {"status": "healthy"}

@api_app.get("/llm-health")
async def llm_health():
    """
    Probe the LLM (OpenAI-compatible) server from within this container.

    The django container sits on internal-only networks and cannot reach the
    LLM host directly, so it proxies this check through the api container,
    which is the only service permitted to reach the inference network.
    """
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1").rstrip("/")
    api_key = os.environ.get("OLLAMA_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        response = requests.get(f"{base_url}/models", headers=headers, timeout=5)
        return {
            "connected": response.status_code == 200,
            "llm_url": base_url,
            "status_code": response.status_code,
        }
    except requests.exceptions.RequestException as e:
        return {
            "connected": False,
            "llm_url": base_url,
            "error": str(e),
        }

@api_app.post("/grade-supplemental-batch")
async def grade_supplemental_batch(body: BackfillRequest):
    """
    Run per-finding supplemental LLM extraction on a batch of existing reports.
    Used by the Django backfill feature to populate *_llm fields on historical records.
    Accepts up to ~30 reports per call; processes them concurrently via the shared
    BatchCXRProcessor worker pool. Returns binary (0/1) findings for each accession.
    """
    if not body.reports:
        return {"results": []}

    df = pd.DataFrame([
        {"ACCESSION_NO": r.accession_no, "TEXT_REPORT": r.text_report}
        for r in body.reports
    ])

    df_result = _batch_processor.process_lunit_batch(df, report_column="TEXT_REPORT")

    _finding_cols = [
        "atelectasis_llm", "calcification_llm", "cardiomegaly_llm", "consolidation_llm",
        "fibrosis_llm", "mediastinal_widening_llm", "nodule_llm", "pleural_effusion_llm",
        "pneumoperitoneum_llm", "pneumothorax_llm",
    ]

    results = []
    for _, row in df_result.iterrows():
        r = {"accession_no": int(row["ACCESSION_NO"])}
        for col in _finding_cols:
            val = row.get(col)
            r[col] = None if (pd.isna(val) if not isinstance(val, bool) else False) or val == "" else int(bool(val))
        # tb column → tb_llm key so Django can use it directly
        tb_val = row.get("tb")
        r["tb_llm"] = None if (pd.isna(tb_val) if not isinstance(tb_val, bool) else False) or tb_val == "" else int(bool(tb_val))
        results.append(r)

    return {"results": results}


# ================================
# STATIC SERVER (Port 1220)
# ================================

static_app = FastAPI(
    title="CXR Analysis Static Server",
    description="Static file server for CXR Analysis upload interface",
    version="1.0.0"
)

# Add CORS middleware
static_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@static_app.get("/", response_class=HTMLResponse)
async def serve_upload_interface():
    """Serve the upload interface HTML page"""
    try:
        # Get the path to the HTML file in the same directory as this script
        html_path = os.path.join(os.path.dirname(__file__), "upload_interface.html")
        
        if not os.path.exists(html_path):
            raise HTTPException(status_code=404, detail="Upload interface not found")
        
        with open(html_path, "r", encoding="utf-8") as f:
            html_content = f.read()
        
        return HTMLResponse(content=html_content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error serving HTML interface: {str(e)}")

@static_app.get("/health")
async def health_check():
    """Simple health check endpoint"""
    return {"status": "healthy", "message": "Static server is running"}

# ================================
# SERVER STARTUP FUNCTIONS
# ================================

def run_api_server(host: str = "0.0.0.0", port: int = 1221):
    """Run the API server (default 0.0.0.0:1221, as in the Docker deployment)."""
    print(f"Starting API Server on {host}:{port}...")
    uvicorn.run(api_app, host=host, port=port, log_level="info")

def run_static_server():
    """Run the static server on port 1220"""
    print("Starting Static Server on port 1220...")
    uvicorn.run(static_app, host="0.0.0.0", port=1220, log_level="info")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CXR Analysis Server")
    parser.add_argument("--api-only", action="store_true",
                        help="Start only the API server on port 1221 (skip static server on 1220)")
    parser.add_argument("--host", default=os.environ.get("API_BIND_HOST", "0.0.0.0"),
                        help="API bind address (default 0.0.0.0; local profile requires 127.0.0.1)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("API_BIND_PORT", "1221")),
                        help="API port (default 1221)")
    args = parser.parse_args()

    startup_errors = local_dev_startup_errors(args.host)
    if startup_errors:
        for message in startup_errors:
            print(f"ERROR: {message}")
        raise SystemExit(2)
    if local_dev_mode() and not args.api_only:
        print("ERROR: PRIMER_LOCAL_DEV runs the API only (use --api-only)")
        raise SystemExit(2)

    print("=" * 60)
    print("🚀 Starting CXR Analysis Combined Server")
    print("=" * 60)
    print("📊 API Server: http://localhost:1221")
    if not args.api_only:
        print("🌐 Upload Interface: http://localhost:1220")
    print("=" * 60)
    
    if args.api_only:
        # Run API server directly on the main thread
        print("📋 API documentation: http://localhost:1221/docs")
        print("\n💡 Press Ctrl+C to stop the server")
        run_api_server(args.host, args.port)
    else:
        # Create threads for both servers
        api_thread = threading.Thread(target=run_api_server, daemon=True)
        static_thread = threading.Thread(target=run_static_server, daemon=True)
        
        # Start both servers
        api_thread.start()
        time.sleep(1)  # Give API server a moment to start
        static_thread.start()
        
        print("✅ Both servers started successfully!")
        print("\n🌐 Open your browser to: http://localhost:1220")
        print("📋 API documentation: http://localhost:1221/docs")
        print("\n💡 Press Ctrl+C to stop both servers")
        
        try:
            # Keep the main thread alive
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n🛑 Shutting down servers...")
            print("✅ Goodbye!")
