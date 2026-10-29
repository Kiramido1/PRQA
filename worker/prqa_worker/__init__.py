"""PR QA Pilot — OpenCode worker.

Owns everything that must be deterministic and testable outside n8n:
review-only policy enforcement, the repository registry, job state
(dedupe, retries), prompt building/validation, result validation,
redaction and report rendering. n8n only orchestrates.
"""

__version__ = "0.1.0"
