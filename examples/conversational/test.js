const WebSocket = require('ws');
const { spawn } = require('child_process');
const { Writable } = require('stream');
const dotenv = require('dotenv');
const Speaker  = require('speaker');
// Configure speaker for linear16 audio playback
const speaker = new Speaker({
  channels: 1,
  bitDepth: 16,
  sampleRate: 48000,
  signed: true
});

dotenv.config();

// Deepgram API configuration
const DEEPGRAM_API_KEY = process.env.DEEPGRAM_API_KEY;
const TTS_TEXT = "Hello, this is a text to speech example using Deepgram.";

// WebSocket URL with parameters
const wsUrl = `wss://api.deepgram.com/v1/speak?model=aura-2-thalia-en&encoding=linear16&sample_rate=48000`;

class AudioPlayer {
  constructor() {
    this.ffplayProcess = null;
    this.audioStream = null;
  }

  start() {
    // Use ffplay to play raw PCM audio
    this.ffplayProcess = spawn('ffplay', [
      '-f', 's16le',        // 16-bit signed little-endian
      '-ar', '48000',       // Sample rate
      '-ac', '1',           // Mono (1 channel)
      '-nodisp',            // No video display
      '-autoexit',          // Exit when done
      '-'                   // Read from stdin
    ], {
      stdio: ['pipe', 'pipe', 'pipe']
    });

    this.audioStream = this.ffplayProcess.stdin;

    this.ffplayProcess.on('error', (error) => {
      console.error('FFplay error:', error);
      console.log('Make sure ffmpeg/ffplay is installed');
    });

    this.ffplayProcess.on('close', (code) => {
      console.log('Audio playback finished');
    });

    console.log('Audio player started');
  }

  write(audioData) {
    if (this.audioStream && !this.audioStream.destroyed) {
      this.audioStream.write(audioData);
    }
  }

  end() {
    if (this.audioStream && !this.audioStream.destroyed) {
      this.audioStream.end();
    }
  }
}

async function main() {
  if (!DEEPGRAM_API_KEY) {
    console.error('Please set DEEPGRAM_API_KEY environment variable');
    return;
  }

  const audioPlayer = new AudioPlayer();

  try {
    // Create WebSocket connection with auth headers
    const ws = new WebSocket(wsUrl, {
      headers: {
        'Authorization': `token ${DEEPGRAM_API_KEY}`
      }
    });

    ws.on('open', () => {
      console.log('WebSocket connection opened');
      
      // Start audio player
      audioPlayer.start();
      
      // Send text message
      const textMessage = JSON.stringify({
        type: 'Speak',
        text: TTS_TEXT
      });
      
      console.log('Sending text:', TTS_TEXT);
      ws.send(textMessage);
      
      // Send flush message to get audio
      const flushMessage = JSON.stringify({
        type: 'Flush'
      });
      
      setTimeout(() => {
        console.log('Sending flush message');
        ws.send(flushMessage);
      }, 100);
    });

    ws.on('message', (data) => {
      // Check if this is binary audio data
      if (Buffer.isBuffer(data)) {
        console.log(`Received and playing audio chunk: ${data.length} bytes`);
        speaker.write(Buffer.from(data));
      } else {
        // Parse JSON response
        try {
          const response = JSON.parse(data.toString());
          console.log('Received message:', response);
        } catch (e) {
          console.log('Received non-JSON message:', data.toString());
        }
      }
    });

    ws.on('close', (code, reason) => {
      console.log(`WebSocket closed: ${code} - ${reason}`);
      audioPlayer.end();
    });

    ws.on('error', (error) => {
      console.error('WebSocket error:', error);
      audioPlayer.end();
    });

    // Close connection after 10 seconds
    setTimeout(() => {
      console.log('Closing WebSocket connection...');
      const closeMessage = JSON.stringify({
        type: 'Close'
      });
      ws.send(closeMessage);
      ws.close();
    }, 10000);

  } catch (error) {
    console.error('Error:', error);
  }
}

main();