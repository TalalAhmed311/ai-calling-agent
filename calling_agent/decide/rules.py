"""Rules decision plane (from preserved bytecode)."""
import marshal
from pathlib import Path
_code = marshal.loads(Path(__file__).resolve().parents[1].joinpath("_bytecode/rules.pyc").read_bytes()[16:]).replace(co_filename="<rules_bytecode>")
exec(_code, globals())
