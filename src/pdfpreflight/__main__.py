import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from viewer.main_window import main

if __name__ == "__main__":
    sys.exit(main())
