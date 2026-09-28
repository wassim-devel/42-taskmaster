The configuration file must allow the user to specify the following, for each program
that is to be supervised:
• The command to use to launch the program
• The number of processes to start and keep running
• Whether to start this program at launch or not
• Whether the program should be restarted always, never, or on unexpected exits
only
• Which return codes represent an "expected" exit status
• How long the program should be running after it’s started for it to be considered
"successfully started"
• How many times a restart should be attempted before aborting
• Which signal should be used to stop (i.e. exit gracefully) the program
• How long to wait after a graceful stop before killing the program
• Options to discard the program’s stdout/stderr or to redirect them to files
• Environment variables to set before launching the program
• A working directory to set before launching the program
• An umask to set before launching the program