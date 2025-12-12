import subprocess
import sys
import os

def run_sshx_commands_cli():
    """
    Runs the SSHX installation and execution commands using subprocess.
    """
    
    # 1. DEFINE THE COMMANDS LIST FIRST (This resolves the NameError)
    commands = [
        "curl -sSf https://sshx.io/get | sh",
        "curl -sSf https://sshx.io/get | sh -s run"
    ]
    
    print("Running SSHX commands on CLI directly...")
    
    # ------------------ Command 1: Installation (Blocking) ------------------
    
    command_1 = commands[0]
    print(f"\n--- Running installation command: {command_1} ---")
    
    try:
        # subprocess.run() is used here because we MUST wait for the installation to finish
        result_1 = subprocess.run(
            command_1,
            shell=True,
            check=True,
            text=True, 
            capture_output=True 
        )
        
        # Print the standard output from the command
        if result_1.stdout:
            print("Output:")
            print(result_1.stdout)

        print("Installation command finished successfully.")

    except subprocess.CalledProcessError as e:
        print(f"Error during command execution: {command_1}", file=sys.stderr)
        if e.stderr:
            print(f"Error details:\n{e.stderr}", file=sys.stderr)
        sys.exit(1)

    except FileNotFoundError:
        print("Error: 'curl' or 'sh' command not found. Ensure they are in your system PATH.", file=sys.stderr)
        sys.exit(1)

    # ------------------ Command 2: Execution (Non-Blocking) ------------------

    command_2 = commands[1]
    print(f"\n--- Running execution command: {command_2} ---")

    try:
        # subprocess.Popen() is used here because 'sshx' starts an interactive session
        # and we DO NOT want the Python script to wait for it to terminate.
        process = subprocess.Popen(
            command_2, 
            shell=True,
            # Direct the output to the terminal so you can see the SSHX session URL
            stdout=sys.stdout, 
            stderr=sys.stderr,
            text=True
        )
        
        print(f"SSHX session started in the background (PID: {process.pid}).")

    except Exception as e:
        print(f"Error during command execution: {command_2}", file=sys.stderr)
        print(f"Error details: {e}", file=sys.stderr)
        sys.exit(1)
        
    # ------------------ Done ------------------
    print("\nDone. The SSHX session is now running. Keep this terminal open or disconnect the session manually to stop it.")

if __name__ == "__main__":
    run_sshx_commands_cli()
