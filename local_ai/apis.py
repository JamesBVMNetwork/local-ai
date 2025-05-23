"""
This module provides a FastAPI application that acts as a proxy or processor for chat completion and embedding requests,
forwarding them to an underlying service running on a local port. It handles both text and vision-based chat completions,
as well as embedding generation, with support for streaming responses.
"""

import os
import logging
import httpx
import asyncio
import base64
import tempfile
import re
import time
import json
import uuid
import subprocess
import signal
import requests
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import StreamingResponse
from functools import lru_cache

# Import schemas from schema.py
from local_ai.schema import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    EmbeddingRequest,
    EmbeddingResponse
)

# Set up logging with both console and file output
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

app = FastAPI()

# Constants for dynamic unload feature
IDLE_TIMEOUT = 600  # 10 minutes in seconds
UNLOAD_CHECK_INTERVAL = 60  # Check every 60 seconds
SERVICE_START_TIMEOUT = 60  # Maximum time to wait for service to start
POOL_CONNECTIONS = 100 # Maximum number of connections in the pool
POOL_KEEPALIVE = 20 # Keep connections alive for 20 seconds
HTTP_TIMEOUT = 600.0  # Default timeout for HTTP requests in seconds

# Cache for service port to avoid repeated lookups
@lru_cache(maxsize=1)
def get_cached_service_port():
    """
    Retrieve the port of the underlying service from the app's state with caching.
    The cache is invalidated when the service info is updated.
    """
    if not hasattr(app.state, "service_info") or "port" not in app.state.service_info:
        logger.error("Service information not set")
        raise HTTPException(status_code=503, detail="Service information not set")
    return app.state.service_info["port"]

# Service Functions
class ServiceHandler:
    """
    Handler class for making requests to the underlying service.
    """
    @staticmethod
    async def get_service_port() -> int:
        """
        Retrieve the port of the underlying service from the app's state.
        """
        try:
            return get_cached_service_port()
        except HTTPException:
            # If cache lookup fails, try direct lookup
            if not hasattr(app.state, "service_info") or "port" not in app.state.service_info:
                logger.error("Service information not set")
                raise HTTPException(status_code=503, detail="Service information not set")
            return app.state.service_info["port"]
    
    @staticmethod
    async def kill_llama_server():
        """
        Kill the llama-server process if it's running.
        """
        try:
            # Get the PID from the service info
            if not hasattr(app.state, "service_info") or "pid" not in app.state.service_info:
                logger.warning("No PID found in service info, cannot kill llama-server")
                return False
                
            pid = app.state.service_info["pid"]
            logger.info(f"Attempting to kill llama-server with PID {pid}")
            
            # Try to kill the process group (more reliable than just the process)
            try:
                # First try to get the process group ID
                pgid = os.getpgid(pid)
                # Kill the entire process group
                os.killpg(pgid, signal.SIGTERM)
                logger.info(f"Successfully sent SIGTERM to process group {pgid}")
            except (ProcessLookupError, OSError) as e:
                logger.warning(f"Could not kill process group: {e}")
                # Fall back to killing just the process
                os.kill(pid, signal.SIGTERM)
                logger.info(f"Successfully sent SIGTERM to process {pid}")
            
            # Wait a moment and check if the process is still running
            await asyncio.sleep(2)
            try:
                os.kill(pid, 0)  # Check if process exists
                # If we get here, the process is still running, try SIGKILL
                logger.warning(f"Process {pid} still running after SIGTERM, sending SIGKILL")
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                # Process is already gone, which is good
                pass
                
            # Remove the PID from service info
            if hasattr(app.state, "service_info"):
                app.state.service_info.pop("pid", None)
                
            return True
        except Exception as e:
            logger.error(f"Error killing llama-server: {str(e)}", exc_info=True)
            return False
    
    @staticmethod
    async def reload_llama_server(service_start_timeout: int = SERVICE_START_TIMEOUT):
        """
        Reload the llama-server process.
        
        Args:
            service_start_timeout: Maximum time in seconds to wait for the service to start
            
        Returns:
            bool: True if the service was successfully started, False otherwise
        """
        try:
            # Get the command to start llama-server from the service info
            if not hasattr(app.state, "service_info") or "running_llm_command" not in app.state.service_info:
                logger.error("No running_llm_command found in service info, cannot reload llama-server")
                return False
                
            running_llm_command = app.state.service_info["running_llm_command"]
            logger.info(f"Reloading llama-server with command: {running_llm_command}")

            logs_dir = Path("logs")
            # Ensure logs directory exists
            logs_dir.mkdir(exist_ok=True)
            
            # Set up log file
            llm_log_stderr = logs_dir / "llm.log"
            llm_process = None
            
            try:
                with open(llm_log_stderr, 'w') as stderr_log:
                    llm_process = subprocess.Popen(
                        running_llm_command,
                        stderr=stderr_log,
                        preexec_fn=os.setsid  # Run in a new process group
                    )
                logger.info(f"LLM logs written to {llm_log_stderr}")
            except Exception as e:
                logger.error(f"Error starting LLM service: {str(e)}", exc_info=True)
                return False
            
            # Wait for the process to start by checking the health endpoint
            port = app.state.service_info["port"]
            start_time = time.time()
            health_check_interval = 1  # Check every second
            
            while time.time() - start_time < service_start_timeout:
                try:
                    response = requests.get(f"http://localhost:{port}/health", timeout=2)
                    if response.status_code == 200:
                        logger.info(f"Service health check passed after {time.time() - start_time:.2f}s")
                        break
                except (requests.RequestException, ConnectionError) as e:
                    # Just wait and try again
                    await asyncio.sleep(health_check_interval)
            
            # Check if the process is running
            if llm_process.poll() is None:
                # Process is running, update the PID in service info
                if hasattr(app.state, "service_info"):
                    app.state.service_info["pid"] = llm_process.pid
                logger.info(f"Successfully reloaded llama-server with PID {llm_process.pid}")
                return True
            else:
                # Process failed to start
                logger.error(f"Failed to reload llama-server: Process exited with code {llm_process.returncode}")
                return False
        except Exception as e:
            logger.error(f"Error reloading llama-server: {str(e)}", exc_info=True)
            return False
    
    @staticmethod
    async def generate_text_response(request: ChatCompletionRequest):
        """
        Generate a response for chat completion requests, supporting both streaming and non-streaming.
        """
        port = await ServiceHandler.get_service_port()

        request.fix_messages()
        # Convert to dict, supporting both Pydantic v1 and v2
        request_dict = request.model_dump() if hasattr(request, "model_dump") else request.dict()

        if request.stream:
            if request.tools:
                # For streaming with tools, we need to get the non-streaming response first
                # and then simulate streaming from it
                stream_request = request_dict.copy()
                stream_request["stream"] = False  # Get non-streaming response first
                
                # Make a non-streaming API call
                response_data = await ServiceHandler._make_api_call(port, "/v1/chat/completions", stream_request)
                
                # Return a simulated streaming response
                return StreamingResponse(
                    ServiceHandler._fake_stream_with_tools(response_data, request.model),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no"
                    }
                )
            
            # Return a streaming response for non-tool requests
            return StreamingResponse(
                ServiceHandler._stream_generator(port, request_dict),
                media_type="text/event-stream"
            )

        # Make a non-streaming API call
        response_data = await ServiceHandler._make_api_call(port, "/v1/chat/completions", request_dict)
        assert isinstance(response_data, dict), "Response data must be a dictionary"
        return ChatCompletionResponse(
            id=response_data.get("id", f"chatcmpl-{uuid.uuid4().hex}"),
            object=response_data.get("object", "chat.completion"),
            created=response_data.get("created", int(time.time())),
            model=request.model,
            choices=response_data.get("choices", [])
        )
    
    @staticmethod
    async def generate_embeddings_response(request: EmbeddingRequest):
        """
        Generate a response for embedding requests.
        """
        port = await ServiceHandler.get_service_port()
        # Convert to dict, supporting both Pydantic v1 and v2
        request_dict = request.model_dump() if hasattr(request, "model_dump") else request.dict()
        response_data = await ServiceHandler._make_api_call(port, "/v1/embeddings", request_dict)
        assert isinstance(response_data, dict), "Response data must be a dictionary"
        return EmbeddingResponse(
            object=response_data.get("object", "list"),
            data=response_data.get("data", []),
            model=request.model
        )
    
    @staticmethod
    async def _make_api_call(port: int, endpoint: str, data: dict) -> dict:
        """
        Make a non-streaming API call to the specified endpoint and return the JSON response.
        Includes retry logic for transient errors.
        """
        
        try:
            response = await app.state.client.post(
                f"http://localhost:{port}{endpoint}", 
                json=data,
                timeout=HTTP_TIMEOUT
            )
            logger.info(f"Received response with status code: {response.status_code}")
            
            if response.status_code != 200:
                error_text = response.text
                logger.error(f"Error: {response.status_code} - {error_text}")
                # Don't retry client errors (4xx), only server errors (5xx)
                if response.status_code < 500:
                    raise HTTPException(status_code=response.status_code, detail=error_text)
            else:
                return response.json()
        except httpx.TimeoutException as e:
            raise HTTPException(status_code=504, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    
    @staticmethod
    async def _stream_generator(port: int, data: dict):
        """
        Generator for streaming responses from the service.
        Yields chunks of data as they are received, formatted for SSE (Server-Sent Events).
        """
        try:
            async with app.state.client.stream(
                "POST", 
                f"http://localhost:{port}/v1/chat/completions", 
                json=data,
                timeout=None  # Streaming needs indefinite timeout
            ) as response:
                if response.status_code != 200:
                    error_text = await response.text()
                    error_msg = f"data: {{\"error\":{{\"message\":\"{error_text}\",\"code\":{response.status_code}}}}}\n\n"
                    logger.error(f"Streaming error: {response.status_code} - {error_text}")
                    yield error_msg
                    return
                    
                async for line in response.aiter_lines():
                    if line:
                        yield f"{line}\n\n"
        except Exception as e:
            logger.error(f"Error during streaming: {e}")
            yield f"data: {{\"error\":{{\"message\":\"{str(e)}\",\"code\":500}}}}\n\n"

    @staticmethod
    async def _fake_stream_with_tools(formatted_response: dict, model: str):
        """
        Generate a fake streaming response for tool-based chat completions.
        This method simulates the streaming behavior by breaking a complete response into chunks.
        
        Args:
            formatted_response: The complete response to stream in chunks
            model: The model name
        """
        
        # Base structure for each chunk
        base_chunk = {
            "id": formatted_response.get("id", f"chatcmpl-{uuid.uuid4().hex}"),
            "object": "chat.completion.chunk",
            "created": formatted_response.get("created", int(time.time())),
            "model": formatted_response.get("model", model),
        }
        
        # Add system_fingerprint only if it exists in the response
        if "system_fingerprint" in formatted_response:
            base_chunk["system_fingerprint"] = formatted_response["system_fingerprint"]

        choices = formatted_response.get("choices", [])
        if not choices:
            # If no choices, return empty response and DONE
            yield f"data: {json.dumps({**base_chunk, 'choices': []})}\n\n"
            yield "data: [DONE]\n\n"
            return

        # Step 1: Initial chunk with role for all choices
        initial_choices = [
            {
                "index": choice["index"],
                "delta": {"role": "assistant", "content": ""},
                "logprobs": None,
                "finish_reason": None
            }
            for choice in choices
        ]
        yield f"data: {json.dumps({**base_chunk, 'choices': initial_choices})}\n\n"

        # Step 2: Chunk with content or tool_calls for all choices
        content_choices = []
        for choice in choices:
            message = choice.get("message", {})
            delta = {}
            
            # For tool calls responses
            if "tool_calls" in message:
                updated_tool_calls = []
                for idx, tool_call in enumerate(message["tool_calls"]):
                    updated_tool_calls.append(tool_call)
                    updated_tool_calls[idx]["index"] = str(idx)
                delta["tool_calls"] = updated_tool_calls
            # For content responses
            elif message.get("content"):
                content = message.get("content", "")
                delta["content"] = content
            else:
                # Empty content/null case
                delta["content"] = ""
                
            if delta:  # Only include choices with content
                content_choices.append({
                    "index": choice["index"],
                    "delta": delta,
                    "logprobs": None,
                    "finish_reason": None
                })
                
        if content_choices:
            yield f"data: {json.dumps({**base_chunk, 'choices': content_choices})}\n\n"

        # Step 3: Final chunk with finish reason for all choices
        finish_choices = [
            {
                "index": choice["index"],
                "delta": {},
                "logprobs": None,
                "finish_reason": choice["finish_reason"]
            }
            for choice in choices
        ]
        yield f"data: {json.dumps({**base_chunk, 'choices': finish_choices})}\n\n"

        # Step 4: End of stream
        yield "data: [DONE]\n\n"

# Request Processor
class RequestProcessor:
    """
    Class for processing requests sequentially using a queue.
    Ensures that only one request is processed at a time to accommodate limitations
    of backends like llama-server that can only handle one request at a time.
    """
    queue = asyncio.Queue()  # Queue for sequential request processing
    processing_lock = asyncio.Lock()  # Lock to ensure only one request is processed at a time
    
    # Define which endpoints need to be processed sequentially
    MODEL_ENDPOINTS = {
        "/v1/chat/completions": (ChatCompletionRequest, ServiceHandler.generate_text_response),
        "/v1/embeddings": (EmbeddingRequest, ServiceHandler.generate_embeddings_response),
        "/chat/completions": (ChatCompletionRequest, ServiceHandler.generate_text_response),
        "/embeddings": (EmbeddingRequest, ServiceHandler.generate_embeddings_response),
    }  # Mapping of endpoints to their request models and handlers
    
    @staticmethod
    async def process_request(endpoint: str, request_data: dict):
        """
        Process a request by adding it to the queue and waiting for the result.
        This ensures requests are processed in order, one at a time.
        Returns a Future that will be resolved with the result.
        """
        request_id = str(uuid.uuid4())[:8]  # Generate a short request ID for tracking
        queue_size = RequestProcessor.queue.qsize()
        
        logger.info(f"[{request_id}] Adding request to queue for endpoint {endpoint} (queue size: {queue_size})")
        
        # Update the last request time
        app.state.last_request_time = time.time()
        
        # Check if we need to reload the llama-server
        if hasattr(app.state, "service_info") and "pid" not in app.state.service_info:
            logger.info(f"[{request_id}] Llama-server not running, reloading...")
            await ServiceHandler.reload_llama_server()
        
        start_wait_time = time.time()
        future = asyncio.Future()
        await RequestProcessor.queue.put((endpoint, request_data, future, request_id, start_wait_time))
        
        # Wait for the future to be resolved
        logger.info(f"[{request_id}] Waiting for result from endpoint {endpoint}")
        result = await future
        
        total_time = time.time() - start_wait_time
        logger.info(f"[{request_id}] Request completed for endpoint {endpoint} (total time: {total_time:.2f}s)")
        
        return result
    
    @staticmethod
    async def process_direct(endpoint: str, request_data: dict):
        """
        Process a request directly without queueing.
        Use this for administrative endpoints that don't require model access.
        """
        request_id = str(uuid.uuid4())[:8]  # Generate a short request ID for tracking
        logger.info(f"[{request_id}] Processing direct request for endpoint {endpoint}")
        
        # Update the last request time
        app.state.last_request_time = time.time()
        
        # Check if we need to reload the llama-server
        if hasattr(app.state, "service_info") and "pid" not in app.state.service_info:
            logger.info(f"[{request_id}] Llama-server not running, reloading...")
            await ServiceHandler.reload_llama_server()
        
        start_time = time.time()
        if endpoint in RequestProcessor.MODEL_ENDPOINTS:
            model_cls, handler = RequestProcessor.MODEL_ENDPOINTS[endpoint]
            request_obj = model_cls(**request_data)
            result = await handler(request_obj)
            
            process_time = time.time() - start_time
            logger.info(f"[{request_id}] Direct request completed for endpoint {endpoint} (time: {process_time:.2f}s)")
            
            return result
        else:
            logger.error(f"[{request_id}] Endpoint not found: {endpoint}")
            raise HTTPException(status_code=404, detail="Endpoint not found")
    
    # Global worker function
    @staticmethod
    async def worker():
        """
        Worker function to process requests from the queue sequentially.
        Only one request is processed at a time.
        """
        logger.info("Request processor worker started")
        processed_count = 0
        
        while True:
            try:
                endpoint, request_data, future, request_id, start_wait_time = await RequestProcessor.queue.get()
                
                wait_time = time.time() - start_wait_time
                queue_size = RequestProcessor.queue.qsize()
                processed_count += 1
                
                logger.info(f"[{request_id}] Processing request from queue for endpoint {endpoint} "
                           f"(wait time: {wait_time:.2f}s, queue size: {queue_size}, processed: {processed_count})")
                
                # Use the lock to ensure only one request is processed at a time
                async with RequestProcessor.processing_lock:
                    processing_start = time.time()
                    
                    if endpoint in RequestProcessor.MODEL_ENDPOINTS:
                        model_cls, handler = RequestProcessor.MODEL_ENDPOINTS[endpoint]
                        try:
                            request_obj = model_cls(**request_data)
                            result = await handler(request_obj)
                            future.set_result(result)
                            
                            processing_time = time.time() - processing_start
                            total_time = time.time() - start_wait_time
                            
                            logger.info(f"[{request_id}] Completed request for endpoint {endpoint} "
                                       f"(processing: {processing_time:.2f}s, total: {total_time:.2f}s)")
                        except Exception as e:
                            logger.error(f"[{request_id}] Handler error for {endpoint}: {str(e)}")
                            future.set_exception(e)
                    else:
                        logger.error(f"[{request_id}] Endpoint not found: {endpoint}")
                        future.set_exception(HTTPException(status_code=404, detail="Endpoint not found"))
                
                RequestProcessor.queue.task_done()
                
                # Log periodic status about queue health
                if processed_count % 10 == 0:
                    logger.info(f"Queue status: current size={queue_size}, processed={processed_count}")
                
            except asyncio.CancelledError:
                logger.info("Worker task cancelled, exiting")
                break  # Exit the loop when the task is canceled
            except Exception as e:
                logger.error(f"Worker error: {str(e)}")
                # Continue working, don't crash the worker

# Unload checker task
async def unload_checker():
    """
    Periodically check if the llama-server has been idle for too long and unload it if needed.
    """
    logger.info("Unload checker task started")
    
    while True:
        try:
            # Wait for the check interval
            await asyncio.sleep(UNLOAD_CHECK_INTERVAL)   
            # Check if the service is running and has been idle for too long
            if (hasattr(app.state, "service_info") and 
                "pid" in app.state.service_info and 
                hasattr(app.state, "last_request_time")):
                
                idle_time = time.time() - app.state.last_request_time
                
                if idle_time > IDLE_TIMEOUT:
                    logger.info(f"Llama-server has been idle for {idle_time:.2f}s, unloading...")
                    await ServiceHandler.kill_llama_server()
            
        except asyncio.CancelledError:
            logger.info("Unload checker task cancelled")
            break
        except Exception as e:
            logger.error(f"Error in unload checker task: {str(e)}", exc_info=True)
            # Continue running despite errors

# Performance monitoring middleware
@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    """
    Middleware that adds a header with the processing time for the request.
    """
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    response.headers["X-Process-Time"] = str(process_time)
    return response

# Dependencies
async def get_background_tasks():
    """Dependency to get background tasks."""
    return BackgroundTasks()

# Lifecycle Events
@app.on_event("startup")
async def startup_event():
    """
    Startup event handler: initialize the HTTP client and start the worker task.
    """
    # Create an asynchronous HTTP client with connection pooling
    limits = httpx.Limits(
        max_connections=POOL_CONNECTIONS,
        max_keepalive_connections=POOL_CONNECTIONS
    )
    app.state.client = httpx.AsyncClient(limits=limits, timeout=HTTP_TIMEOUT)
    
    # Initialize the last request time
    app.state.last_request_time = time.time()
    
    # Start the worker
    app.state.worker_task = asyncio.create_task(RequestProcessor.worker())
    
    # Start the unload checker task
    app.state.unload_checker_task = asyncio.create_task(unload_checker())
    
    logger.info("Service started successfully")

@app.on_event("shutdown")
async def shutdown_event():
    """
    Shutdown event handler: close the HTTP client and cancel the worker task.
    """
    logger.info("Shutting down service")
    
    # Close the HTTP client
    if hasattr(app.state, "client"):
        await app.state.client.aclose()
    
    # Cancel the worker task
    if hasattr(app.state, "worker_task"):
        app.state.worker_task.cancel()
        try:
            await app.state.worker_task  # Wait for the worker to finish
        except asyncio.CancelledError:
            pass  # Handle cancellation gracefully
    
    # Cancel the unload checker task
    if hasattr(app.state, "unload_checker_task"):
        app.state.unload_checker_task.cancel()
        try:
            await app.state.unload_checker_task  # Wait for the task to finish
        except asyncio.CancelledError:
            pass  # Handle cancellation gracefully
    
    # Kill the llama-server if it's running
    if hasattr(app.state, "service_info") and "pid" in app.state.service_info:
        await ServiceHandler.kill_llama_server()
    
    logger.info("Service shutdown complete")

# API Endpoints
@app.get("/health")
@app.get("/v1/health")
async def health():
    """
    Health check endpoint.
    Returns a simple status to indicate the service is running.
    This endpoint bypasses the request queue for immediate response.
    """
    # Invalidate the service port cache periodically
    get_cached_service_port.cache_clear()
    
    # Check if the service info is set
    if not hasattr(app.state, "service_info"):
        return {"status": "starting", "message": "Service info not set yet"}
    
    # Update the last request time
    app.state.last_request_time = time.time()
    
    return {"status": "ok", "service": app.state.service_info.get("family", "unknown")}


@app.post("/update")
async def update(request: dict):
    """
    Update the service information in the app's state.
    Stores the provided request data for use in determining the service port.
    This endpoint bypasses the request queue for immediate response.
    """
    app.state.service_info = request
    # Invalidate the cache when service info is updated
    get_cached_service_port.cache_clear()
    logger.info(f"Updated service info: {request.get('family', 'unknown')} on port {request.get('port', 'unknown')}")
    return {"status": "ok", "message": "Service info updated successfully"}

# Modified endpoint handlers for model-based endpoints
@app.post("/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    """
    Endpoint for chat completion requests.
    Uses the request queue to ensure only one model request is processed at a time.
    """
    # Convert to dict, supporting both Pydantic v1 and v2
    request_dict = request.model_dump() if hasattr(request, "model_dump") else request.dict()
    return await RequestProcessor.process_request("/chat/completions", request_dict)

@app.post("/embeddings")
async def embeddings(request: EmbeddingRequest):
    """
    Endpoint for embedding requests.
    Uses the request queue to ensure only one model request is processed at a time.
    """
    # Convert to dict, supporting both Pydantic v1 and v2
    request_dict = request.model_dump() if hasattr(request, "model_dump") else request.dict()
    return await RequestProcessor.process_request("/embeddings", request_dict)

@app.post("/v1/chat/completions")
async def v1_chat_completions(request: ChatCompletionRequest):
    """
    Endpoint for chat completion requests (v1 API).
    Uses the request queue to ensure only one model request is processed at a time.
    """
    # Convert to dict, supporting both Pydantic v1 and v2
    request_dict = request.model_dump() if hasattr(request, "model_dump") else request.dict()
    return await RequestProcessor.process_request("/v1/chat/completions", request_dict)

@app.post("/v1/embeddings")
async def v1_embeddings(request: EmbeddingRequest):
    """
    Endpoint for embedding requests (v1 API).
    Uses the request queue to ensure only one model request is processed at a time.
    """
    # Convert to dict, supporting both Pydantic v1 and v2
    request_dict = request.model_dump() if hasattr(request, "model_dump") else request.dict()
    return await RequestProcessor.process_request("/v1/embeddings", request_dict)