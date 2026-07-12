"""`python -m sfdc_mcp` entry point — delegates to the same `main()` the console
script (`sfdc-mcp-server`) uses, so both invocation styles behave identically."""

from sfdc_mcp.server import main

if __name__ == "__main__":
    main()
