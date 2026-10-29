"""Alias of the production rubric, prqa_worker.qa_rubric (the single definition used by the worker's /prompts/finalize,
the evaluators and the benchmark). `import qa_rubric` yields that same module object, private helpers included."""
import sys

from prqa_worker import qa_rubric as _impl

sys.modules[__name__] = _impl
