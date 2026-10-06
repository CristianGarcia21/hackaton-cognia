"""Sistema de herramientas: decora una función con @tool y ya la puede usar un agente.

    @tool
    def clima(ciudad: str, dias: int = 1) -> str:
        '''Consulta el clima de una ciudad.

        Args:
            ciudad: nombre de la ciudad
            dias: cuántos días de pronóstico
        '''

El schema JSON se genera a partir de los type hints y del docstring.
"""

import inspect
import re
import types
import typing
from collections.abc import Callable
from dataclasses import dataclass

_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}


@dataclass
class Tool:
    name: str
    description: str
    fn: Callable[..., typing.Any]
    parameters: dict

    def __call__(self, **kwargs):
        return self.fn(**kwargs)

    def to_openai(self) -> dict:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


def _json_type(annotation) -> dict:
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        return {"type": "string", "enum": list(typing.get_args(annotation))}
    if origin is list:
        args = typing.get_args(annotation)
        return {"type": "array", "items": _json_type(args[0]) if args else {"type": "string"}}
    if origin in (typing.Union, types.UnionType):  # str | None -> str
        non_none = [a for a in typing.get_args(annotation) if a is not type(None)]
        return _json_type(non_none[0]) if non_none else {"type": "string"}
    return {"type": _TYPES.get(origin or annotation, "string")}


def _parse_docstring(doc: str) -> tuple[str, dict[str, str]]:
    doc = inspect.cleandoc(doc or "")
    summary, _, rest = doc.partition("Args:")
    params = dict(re.findall(r"^\s*(\w+)\s*(?:\(.*?\))?:\s*(.+)$", rest, re.MULTILINE))
    return summary.strip(), params


def tool(fn: Callable | None = None, *, name: str | None = None, description: str | None = None):
    """Decorador que convierte una función en Tool. Se puede usar como @tool o @tool(name=...)."""

    def wrap(f: Callable) -> Tool:
        summary, param_docs = _parse_docstring(f.__doc__)
        hints = typing.get_type_hints(f)
        properties, required = {}, []
        for pname, param in inspect.signature(f).parameters.items():
            schema = _json_type(hints.get(pname, str))
            if pname in param_docs:
                schema["description"] = param_docs[pname]
            properties[pname] = schema
            if param.default is inspect.Parameter.empty:
                required.append(pname)
        return Tool(
            name=name or f.__name__,
            description=description or summary or f.__name__,
            fn=f,
            parameters={"type": "object", "properties": properties, "required": required},
        )

    return wrap(fn) if fn else wrap
