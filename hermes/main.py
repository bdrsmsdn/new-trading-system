import sys
from hermes.cli import main as cli_main

def main():
    """Backward compatible entry point for hermes."""
    # Convert old dash args to commands if provided without `cli`
    args = sys.argv[1:]
    if "--daemon" in args:
        sys.argv = [sys.argv[0], "daemon"]
    elif "--one-shot" in args:
        sys.argv = [sys.argv[0], "one-shot"]
    elif "--analyze" in args:
        if "--all" in args:
            sys.argv = [sys.argv[0], "analyze", "--all"]
        else:
            sys.argv = [sys.argv[0], "analyze"]
    
    cli_main()

if __name__ == "__main__":
    main()
