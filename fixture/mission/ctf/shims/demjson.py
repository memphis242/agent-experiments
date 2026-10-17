# Re-export demjson3 under the legacy module name that CTF imports.
# CTF uses 'demjson.jsonlint'; demjson 2.2.4 does not build on Python 3.12.
from demjson3 import *  # noqa: F401,F403
import demjson3 as _d
jsonlint = _d.jsonlint
