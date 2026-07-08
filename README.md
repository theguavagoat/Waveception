# ****Waveception****

## What it is:
	        
Waveception is enterprise middleware that synchronizes access control events with 
	 one or more video management systems using REST APIs. 
     It automatically creates searchable video bookmarks that allow operators to quickly 
	 locate archived footage associated with access control events.

## Features:

- REST API integration
- SQLite event persistence
- Automatic retry queue
- Windows Service support
- Configuration GUI
- Multiple VMS support
- Event deduplication
- Automatic reconnect logic
- Door-to-camera mapping
- Configurable bookmark pre/post roll


## Architecture:
		
    Inception

      		│
 		REST API
      		│
      		▼

	+----------------------+
	|     Waveception      |
	|                      |
	|  Event Processing    |
	|  SQLite Queue        |
	|  Retry Logic         |
	+----------------------+
	
	   │              │
	REST API       REST API
	   │		   │

	   ▼             ▼

	Hanwha WAVE   ISS SecurOS

## Why I Built It:
	
A very common issue for operators of Access Control Systems (ACS) and Video Management Systems (VMS) is correlating access 
	control events with archived video.

For example: A company uses their door swipes as a clock in/out system for their employees. John Doe is normally scheduled 
	to arrive for work at 9am. John has gotten into the habit of routinely showing up late and passing his card off to another 
	employee to clock him in for the day before he actually shows up to work. John's manager starts to suspect him of showing up 
	late but the access control system shows John Doe clocking into work on time. 

PREVIOUSLY
		The ACS/VMS operator(s) would have to log into the ACS, note the time of the events when John Doe swipes his card on the 
		clock in reader, and then log into the VMS and manually search for footage at the time of each of the corresponding events.

NOW
		The ACS/VMS operator can log into the VMS and search for access event bookmarks based on cardholder
		name, door name, or both.

RESULT
		This eliminates the need to manually correlate access logs with archived video, reducing investigation
		time from hours to minutes.



