We're putting together a project to stream live video from fishing vessels, up to a Cloudflare RTMPS live stream, which we then embed in a
website.

We have a POE security camera, and a Raspberry Pi 5 (Cana Kit, with heat sink etc).

We also have a switch if we need it. Internet will be provided by Starlink (or a standard home router while we're testing).

I'm thinking we need to create a small application to go on the Raspberry Pi, with FFMPEG to convert the video to the correct 
format and point it at the Cloudflare RTMPS server.

I think we'll need some sort of web interface to control settings, check on the health etc

It needs to be versatile, robust and easy to install. We likely won't be able to install it ourselves, and will have to easily
explain how to set it up or is of standard technical ability.

We need to think about how it auto powers on, cases where we need to repoint the stream to a new endpoint, what happens if it
crashes, loses power etc etc

Let's come up with a robust plan, with all the features we need.

I'm not an expert in this, so open to suggestions. Please consider anything I may not have thought about.