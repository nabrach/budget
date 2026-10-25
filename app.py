"""Start the budget app: python3 app.py"""

from budgetapp.config import load_config
from budgetapp.server import run

if __name__ == "__main__":
    run(load_config())
