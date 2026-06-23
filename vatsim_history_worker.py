import sys
import os
from pathlib import Path

# 1. Find the plugin root (the folder containing vatsim_data.py and .env)
script = Path(__file__).resolve()
plugin_root = script.parent
while not (plugin_root / "vatsim_data.py").exists():
    if plugin_root.parent == plugin_root:
        raise RuntimeError("Cannot locate plugin root – vatsim_data.py not found")
    plugin_root = plugin_root.parent

# 2. Load .env from the plugin root
from dotenv import load_dotenv
load_dotenv(plugin_root / '.env')
VATSIM_DATA_URL = os.getenv("VATSIM_DATA_URL")

# 3. Import VatsimData safely, even if it uses relative imports internally
# We need to import it as part of its package. The package name is the folder name.
package_name = plugin_root.name
parent_of_plugin = plugin_root.parent

# Add parent directory to sys.path to make the package importable
if str(parent_of_plugin) not in sys.path:
    sys.path.insert(0, str(parent_of_plugin))

# Now import using the full package path
try:
    # Try package import first (handles any internal relative imports in vatsim_data.py)
    exec(f"from {package_name}.vatsim_data import VatsimData")
except ImportError:
    # Fallback: maybe vatsim_data.py has no relative imports, direct import works
    if str(plugin_root) not in sys.path:
        sys.path.insert(0, str(plugin_root))
    from vatsim_data import VatsimData

# 4. Run your logic
print("Fetching VATSIM Data")
vatsim_data = VatsimData(VATSIM_DATA_URL)
vatsim_data.fetch_vatsim_data()