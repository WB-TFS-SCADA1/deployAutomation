import os
import sys
import stat
import time
import argparse
import tempfile
import shutil
from pathlib import Path
import getpass
import paramiko
from paramiko import SSHClient, AutoAddPolicy


class DeploymentManager:
    def __init__(self, server_host, server_user, server_password, env_dir=None):
        self.server_host = server_host
        self.server_user = server_user
        self.server_password = server_password
        self.temp_dir = None
        self.ssh = None
        self.sftp = None
        # Directory containing .env files (default to user's home directory)
        self.env_dir = env_dir or os.path.expanduser('~/.script_envs')

    def connect(self):
        """Establish SSH and SFTP connections"""
        self.ssh = SSHClient()
        self.ssh.set_missing_host_key_policy(AutoAddPolicy())
        self.ssh.connect(
            self.server_host,
            username=self.server_user,
            password=self.server_password
        )
        self.sftp = self.ssh.open_sftp()

    def run_remote_command(self, command):
        """Execute command on remote server"""
        stdin, stdout, stderr = self.ssh.exec_command(command)
        exit_status = stdout.channel.recv_exit_status()
        return {
            'returncode': exit_status,
            'stdout': stdout.read().decode('utf-8'),
            'stderr': stderr.read().decode('utf-8')
        }

    def upload_file(self, local_path, remote_path):
        """Upload a file to the remote server"""
        try:
            # Create remote directory if it doesn't exist
            remote_dir = os.path.dirname(remote_path)
            self.run_remote_command(f"mkdir -p {remote_dir}")

            # Upload file
            self.sftp.put(local_path, remote_path)
            return True
        except Exception as e:
            print(f"Failed to upload {local_path}: {str(e)}")
            return False

    def clone_repo(self, repo_url, branch='master'):
        """Clone specific branch of repository"""
        self.temp_dir = tempfile.mkdtemp()
        import subprocess
        subprocess.run(['git', 'clone', '-b', branch, repo_url, self.temp_dir], check=True)
        return self.temp_dir

    def handle_env_file(self, script_name, remote_script_path):
        """Handle .env file for the script"""
        # Check for .env file in the predefined directory
        env_file_path = os.path.join(self.env_dir, f"{script_name}.env")

        if os.path.exists(env_file_path):
            print(f"Found .env file for {script_name}")
            remote_env_path = f"/opt/{script_name}/.env"
            return self.upload_file(env_file_path, remote_env_path)
        else:
            print(f"No .env file found for {script_name} at {env_file_path}")
            create_env = input("Would you like to create one now? (y/n): ").lower().strip()
            if create_env == 'y':
                # Create directory if it doesn't exist
                os.makedirs(self.env_dir, exist_ok=True)

                print("Enter your environment variables (one per line)")
                print("Format: KEY=VALUE")
                print("Press Enter twice when done")

                env_contents = []
                while True:
                    line = input().strip()
                    if not line:
                        break
                    env_contents.append(line)

                # Save the new .env file
                with open(env_file_path, 'w') as f:
                    f.write('\n'.join(env_contents))

                print(f"Created .env file at {env_file_path}")
                remote_env_path = f"/opt/{script_name}/.env"
                return self.upload_file(env_file_path, remote_env_path)

        return True

    def deploy_script(self, script_path, script_name):
        """Deploy a script to the server"""
        print(f"\nDeploying {script_name}...")

        # Create directory in /opt with sudo
        create_dir_cmd = f"echo '{self.server_password}' | sudo -S mkdir -p /opt/{script_name}"
        print(f"Creating directory with command: {create_dir_cmd}")
        result = self.run_remote_command(create_dir_cmd)
        print(f"Create directory result - returncode: {result['returncode']}")
        print(f"stdout: {result['stdout']}")
        print(f"stderr: {result['stderr']}")
        if result['returncode'] != 0:
            print(f"Failed to create directory: {result['stderr']}")
            return False

        # Set permissions
        chown_cmd = f"echo '{self.server_password}' | sudo -S chown {self.server_user}:{self.server_user} /opt/{script_name}"
        print(f"Setting permissions with command: {chown_cmd}")
        result = self.run_remote_command(chown_cmd)
        print(f"Set permissions result - returncode: {result['returncode']}")
        print(f"stdout: {result['stdout']}")
        print(f"stderr: {result['stderr']}")
        if result['returncode'] != 0:
            print(f"Failed to set permissions: {result['stderr']}")
            return False

        # Copy files
        for root, _, files in os.walk(script_path):
            for file in files:
                local_path = os.path.join(root, file)
                relative_path = os.path.relpath(local_path, script_path)
                remote_path = f"/opt/{script_name}/{relative_path}"

                if not self.upload_file(local_path, remote_path):
                    return False

        # Handle .env file (now using the correct script_name)
        if not self.handle_env_file(script_name, script_path):
            return False

        # Check if requirements.txt exists before setting up virtual environment
        if os.path.exists(os.path.join(script_path, 'requirements.txt')):
            setup_commands = [
                f"cd /opt/{script_name}",
                "python3.12 -m venv venv",
                "source venv/bin/activate",
                "pip install --upgrade pip",
                "pip install -r requirements.txt"
            ]
            result = self.run_remote_command(" && ".join(setup_commands))
            if result['returncode'] != 0:
                print(f"Failed to set up virtual environment: {result['stderr']}")
                return False

        # Create runner script
            runner_script = f"""#!/usr/bin/bash
            # Set the PATH variable to include the default system paths
            export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

            # Full path to the Python interpreter and log file
            PYTHON_INTERPRETER="/opt/{script_name}/venv/bin/python3"

            # Create logs directory if it doesn't exist
            mkdir -p /opt/{script_name}/logs

            # Run the Python script within the virtual environment and log output
            cd /opt/{script_name}
            source venv/bin/activate
            $PYTHON_INTERPRETER /opt/{script_name}/main.py &
            """
            # Write the file with Unix line endings
            runner_path = os.path.join(script_path, "run_script.sh")
            with open(runner_path, 'w', newline='\n') as f:
                f.write(runner_script)

            # Upload and set up runner script
            remote_runner_path = f"/opt/{script_name}/run_script.sh"
            if not self.upload_file(runner_path, remote_runner_path):
                return False

            # Make runner script executable and ensure Unix line endings on remote
            setup_commands = [
                f"chmod +x /opt/{script_name}/run_script.sh",
                f"dos2unix /opt/{script_name}/run_script.sh"  # Convert to Unix line endings
            ]

            for cmd in setup_commands:
                result = self.run_remote_command(cmd)
                if result['returncode'] != 0:
                    print(f"Failed to execute {cmd}: {result['stderr']}")
                    # If dos2unix fails, it might not be installed
                    if 'dos2unix' in cmd:
                        print("Installing dos2unix...")
                        install_cmd = f"echo '{self.server_password}' | sudo -S apt-get update && echo '{self.server_password}' | sudo -S apt-get install -y dos2unix"
                        result = self.run_remote_command(install_cmd)
                        if result['returncode'] == 0:
                            # Try the dos2unix command again
                            result = self.run_remote_command(cmd)
                            if result['returncode'] != 0:
                                print(f"Failed to convert line endings: {result['stderr']}")
                                return False
                    else:
                        return False

        # Update crontab if cron.txt exists
        cron_path = os.path.join(script_path, "cron.txt")
        if os.path.exists(cron_path):
            with open(cron_path, 'r') as f:
                cron_schedule = f.read().strip()

            cron_cmd = f'(crontab -l 2>/dev/null | grep -v "{script_name}" ; echo "{cron_schedule} /opt/{script_name}/run_script.sh") | crontab -'
            result = self.run_remote_command(cron_cmd)
            if result['returncode'] != 0:
                print(f"Failed to update crontab: {result['stderr']}")
                return False

        print(f"Successfully deployed {script_name}")
        return True

    def force_remove_readonly(self, func, path, exc_info):
        """Error handler for shutil.rmtree to handle readonly files"""
        # Make the file writable and try again
        os.chmod(path, stat.S_IWRITE)
        func(path)

    def cleanup(self):
        """Clean up connections and temporary directory with Windows fixes"""
        if self.sftp:
            self.sftp.close()
        if self.ssh:
            self.ssh.close()

        if self.temp_dir and os.path.exists(self.temp_dir):
            retry_count = 3
            for i in range(retry_count):
                try:
                    # On Windows, we need to handle readonly files
                    shutil.rmtree(self.temp_dir, onerror=self.force_remove_readonly)
                    break
                except Exception as e:
                    if i == retry_count - 1:  # Last attempt
                        print(f"Warning: Could not remove temporary directory {self.temp_dir}")
                        print(f"You may need to manually delete it later")
                    else:
                        # Wait a bit before retrying
                        time.sleep(1)


    def verify_python_version(self):
        """Verify that Python 3.12 is available on the remote server"""
        result = self.run_remote_command("command -v python3.12")
        if result['returncode'] != 0:
            raise RuntimeError("Python 3.12 is not installed on the remote server")

        # Verify version
        result = self.run_remote_command("python3.12 --version")
        if result['returncode'] != 0:
            raise RuntimeError("Failed to get Python version")

        version = result['stdout'].strip()
        print(f"Remote Python version: {version}")

        return True


def main():
    parser = argparse.ArgumentParser(description='Deploy Python scripts from GitHub to server')
    parser.add_argument('repo_url', help='GitHub repository URL')
    parser.add_argument('--host', help='Server hostname')
    parser.add_argument('--user', help='Server username')
    parser.add_argument('--branch', default='master', help='Git branch to deploy (default: master)')
    parser.add_argument('--env-dir', help='Directory containing .env files (default: ~/.script_envs)')

    args = parser.parse_args()

    # Get password securely
    server_password = getpass.getpass('Enter server password: ')

    deployer = None
    try:
        print("Initializing deployment manager...")
        deployer = DeploymentManager(args.host, args.user, server_password, args.env_dir)

        print("Establishing SSH connection...")
        deployer.connect()

        print("Verifying Python version...")
        deployer.verify_python_version()

        print("Cloning repository...")
        repo_path = deployer.clone_repo(args.repo_url, args.branch)
        print(f"Repository cloned to: {repo_path}")

        print("Checking repository structure...")
        # Check if main.py exists in the root directory
        if os.path.exists(os.path.join(repo_path, 'main.py')):
            # Get the repository name from the URL
            repo_name = os.path.basename(args.repo_url.rstrip('.git'))
            print(f"Found main.py in repository root. Using repo name: {repo_name}")
            if not deployer.deploy_script(repo_path, repo_name):  # Pass repo_name as a parameter
                print("Deployment failed!")
                raise Exception("Deployment failed")
        else:
            print("No main.py found in repository root!")
            print("Repository structure:")
            for root, dirs, files in os.walk(repo_path):
                level = root.replace(repo_path, '').count(os.sep)
                indent = ' ' * 4 * level
                print(f"{indent}{os.path.basename(root)}/")
                subindent = ' ' * 4 * (level + 1)
                for f in files:
                    print(f"{subindent}{f}")

    except Exception as e:
        print(f"An error occurred: {str(e)}")
        raise
    finally:
        if deployer:
            deployer.cleanup()


if __name__ == "__main__":
    main()


    #python deploy.py https://github.com/WB-TFS-SCADA1/lightningStrikes.git --host 10.20.1.4 --user scadaadmin --env-dir "C:\Users\Chris.Morris\PycharmProjects\.script_envs"
