-- Rynhult Setup: makes this Mac an additional machine of an existing Rynhult. deploy/macos/build-pkg.sh compiles it with
-- osacompile into /Applications/ACC Setup.app; the package's postinstall opens it as the signed-in user, and it can be
-- opened again at any time (to enroll again, or after an update). Every step is deploy/macos/acc-setup.sh, which
-- parses the pasted command strictly and never runs it as given. Kept to ASCII: osacompile reads the source as text.

property setupScript : "/Library/Application Support/ACC/src/deploy/macos/acc-setup.sh"

on run
	set arrow to character id 8594
	set logFile to POSIX path of (path to home folder) & "Library/Logs/ACC/setup.log"
	set setup to quoted form of setupScript

	try
		do shell script "test -x " & setup
	on error
		display alert "Rynhult Setup can't find Rynhult" message "It should be in /Library/Application Support/ACC/src. Install the Rynhult package again." as critical
		return
	end try

	-- macOS, not root, Homebrew: nothing is installed before these pass.
	try
		do shell script setup & " check"
	on error errMsg number errNum
		if errNum is 3 then
			set choice to button returned of (display alert "Rynhult needs Homebrew" message errMsg buttons {"Cancel", "Open brew.sh"} default button "Open brew.sh" cancel button "Cancel")
			if choice is "Open brew.sh" then open location "https://brew.sh"
		else
			display alert "This Mac can't be set up" message errMsg as critical
		end if
		return
	end try

	set intro to "Paste the command from Host " & arrow & " Machines " & arrow & " Add machine on your Rynhult." & return & return & "This Mac becomes an additional machine of that Rynhult. Sessions run as you (" & (short user name of (system info)) & ")."
	set enrolled to do shell script setup & " status"
	set buttonList to {"Cancel", "Set up"}
	if enrolled is not "" then
		set intro to "This Mac is already " & enrolled & ". Select Update only to install this version of Rynhult, or add it on your Rynhult again and paste the new command to enroll it again." & return & return & intro
		set buttonList to {"Cancel", "Update only", "Set up"}
	end if
	set prompt to intro
	set updateOnly to false
	repeat
		-- Cancel ends the script here (error -128), as it should.
		set reply to display dialog prompt default answer "" buttons buttonList default button "Set up" cancel button "Cancel" with title "Rynhult Setup" with icon note
		if button returned of reply is "Update only" then
			set updateOnly to true
			exit repeat
		end if
		set pasted to text returned of reply
		try
			do shell script setup & " parse " & quoted form of pasted
			exit repeat
		on error errMsg
			set prompt to "That isn't the command from Add machine: " & errMsg & "." & return & return & intro
		end try
	end repeat

	if updateOnly then
		set progress total steps to 1
	else
		set progress total steps to 2
	end if
	set progress completed steps to 0
	set progress description to "Setting up Rynhult on this Mac"
	try
		set progress additional description to "Installing what Rynhult needs (Python, tmux, git, uv) and Rynhult itself. The first time this takes several minutes."
		with timeout of 7200 seconds
			set summary to do shell script setup & " install --log"
		end timeout
		set progress completed steps to 1
		if not updateOnly then
			set progress additional description to "Enrolling with your Rynhult"
			with timeout of 600 seconds
				set summary to do shell script setup & " enroll --log -- " & quoted form of pasted
			end timeout
			set progress completed steps to 2
		end if
	on error errMsg number errNum
		if errNum is -128 then return
		set choice to button returned of (display alert "Rynhult Setup failed" message errMsg & return & return & "The full log is " & logFile buttons {"Show Log", "OK"} default button "OK" as critical)
		if choice is "Show Log" then do shell script "open " & quoted form of logFile
		return
	end try
	display dialog summary buttons {"OK"} default button "OK" with title "Rynhult Setup" with icon note
end run
