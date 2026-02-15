# [MeshMap](https://thevillages.duckdns.org/meshmap)
A map of Meshtastic nodes in The Villages, FL as seen by the official Meshtastic MQTT server.
This implementation is a simple extension of Brian Shea's meshmap.net for node topology management at The Villages, FL.

## Features
- Shows all position-reporting nodes heard by Meshtastic's [official MQTT server](https://meshtastic.org/docs/configuration/module/mqtt/#connect-to-the-default-public-server)
- Includes nodes self-reporting to MQTT or heard by another node reporting to MQTT
- Node data is updated every minute
- Nodes are removed from the map if their position is not updated after one year or if OK to MQTT is disabled or if the device role is set to CLIENT_MUTE
- Search for nodes by name or device ID

### Known Issues
When the map is displayed for the very first time (or browser data storage is initialized), a world 
map is displayed rather than a map of the Villages, FL.  You must click and zoom to your preferred 
map presentation for the Villages.  Your browser will reposition to this presentation on subsequent 
calls.

Panning to other parts of the world is allowed. But since only routers, gateways and clients are displayed 
on the map, the node icons for other roles outside The Villages may not be correct. 

## FAQs

### How do I get my node on the map?
These are general requirements.
- First, make sure you are running a [recent firmware](https://meshtastic.org/downloads/) version
- Use the default primary channel and encryption key
- Enable "OK to MQTT" in LoRa configuration (signaling you want your messages uplinked via MQTT)
- Enable position reports from your node
  - This may mean enabling your node's built-in GPS, sharing your phone's location via the app, or setting a fixed position
  - Ensure "Position enabled" is enabled on the primary channel
  - Precise locations are filtered (see important update below -- the default precision will work)

If your node can be heard by another node already reporting to MQTT, that's it!

#### Important update as of August, 2024
Meshtastic has [made a change to their MQTT server](https://meshtastic.org/blog/recent-public-mqtt-broker-changes/):

> Only position packets with imprecise location information [10-16 bits] will be passed to the topic, ensuring that sensitive data is not exposed.

The most accurate resolution that conforms to this specification is 364 meters/1194 feet.

#### To enable MQTT reporting
- Enable the MQTT module, using all default settings, with 'thevillages' root topic
- Configure your node to connect to wifi or otherwise connect to the internet
- Enable MQTT uplink on your primary channel
  - It is not necessary, and not recommended unless you know what you're doing, to enable MQTT downlink

### Does the map allow manual/self-reported nodes (not over MQTT)?
No.

### Can you add this awesome new feature I just came up with? (Or you'd like to report a bug)
Let's discuss it at a breakfast meeting or catch-up session.

### Can I use your code for my own map in my own region?
Sure! Go for it!!

## Installation Instructions

The detailed instructions here are for the installation of a Raspberry PI webserver that displays a map 
of The Villages, FL with colored pins representing the roles and locations of the meshtastic radios owned 
by village residents. Adjustments would have to be made for differences in hardware (raspberry pi), 
webserver (apache2) or geographic location (The Villages, FL).

### Pre-installation Requirements

    Go
    docker
    apache2
    git
    bash

### Command Line Entries on Raspberry Pi

    sudo apt update
    sudo apt upgrade
    [install any of the required apps from the above list not already installed on your raspberrry pi]

### Install backend database: 

    cd ~
    git clone https://github.com/jcerullo/meshmap.net.git
    cd meshmap.net
    git init    
    cd meshmap.net/scripts
    sudo bash build-meshobserv.sh                   [build a backend docker container]
    sudo bash run-meshobserv.sh                     [run the docker container]
    sudo docker ps                                  [check that the docker container is up and running]
    sudo docker exec -it meshobserv sh              
    '# cd data/meshmap.net/website                  [position to the internal location of the database, nodes.json]
    '# cat nodes.json                               [check that the file, nodes.json, exists and is changing]
    '# exit

### Install frontend webserver:

    cd /var/www/html
    sudo mkdir meshmap
    cd ~/meshmap.net/website
    sudo cp * /var/www/html/meshmap                 [From your browser enter http://[RaspberryPi_IP address]/meshmap to display an unmodified meshmap]

### Customize pin icons: colors, size, roles

    cd /var/www/html/meshmap
    sudo nano index.html
    search for “changeme” to position to recommended code change locations 

### Modify Author

If you have an external github repository, change the author in these files:

    README.md
    mqtt.go
    go.mod
    meshobserv.go
    index.html