import asyncio
import websockets
import os
import json
from dotenv import load_dotenv
from .base_transcriber import BaseTranscriber
from agents.helpers.logger_config import configure_logger
from agents.helpers.utils import create_ws_data_packet

logger = configure_logger(__name__)
load_dotenv()


class DeepgramTranscriber(BaseTranscriber):
    def __init__(self, provider, input_queue=None, model='deepgram', stream=True, language="en", endpointing="400"):
        super().__init__(input_queue)
        self.endpointing = endpointing
        self.language = language
        self.stream = stream
        self.provider = provider
        self.model = 'deepgram'

    def get_deepgram_ws_url(self):
        websocket_url = (f"wss://api.deepgram.com/v1/listen?encoding=linear16&sample_rate=16000&channels=1"
                         f"&filler_words=true&endpointing={self.endpointing}")

        if self.provider == 'twilio':
            websocket_url = (f"wss://api.deepgram.com/v1/listen?model=nova-2&encoding=mulaw&sample_rate=8000&channels"
                             f"=1&filler_words=true&endpointing={self.endpointing}")

        if "en" not in self.language:
            websocket_url += '&language={}'.format(self.language)
        logger.info('Websocket URL: {}'.format(websocket_url))
        return websocket_url

    async def send_heartbeat(self, ws):
        try:
            while True:
                data = {'type': 'KeepAlive'}
                await ws.send(json.dumps(data))
                await asyncio.sleep(5)  # Send a heartbeat message every 5 seconds
        except Exception as e:
            logger.error('Error while sending: ' + str(e))
            raise Exception("Something went wrong while sending heartbeats to {}".format(self.model))

    async def sender(self, ws):
        try:
            while True:
                ws_data_packet = await self.input_queue.get()
                audio_data = ws_data_packet.get('data')
                self.meta_info = ws_data_packet.get('meta_info')
                await ws.send(audio_data)
        except Exception as e:
            logger.error('Error while sending: ' + str(e))
            raise Exception("Something went wrong")

    async def receiver(self, ws):
        curr_message = ""
        async for msg in ws:
            try:
                logger.info(f"Got response from {self.model} {msg}")
                msg = json.loads(msg)
                transcript = msg['channel']['alternatives'][0]['transcript']

                self.update_meta_info(transcript)

                if transcript and len(transcript.strip()) != 0:
                    if await self.signal_transcription_begin(msg):
                        yield create_ws_data_packet("TRANSCRIBER_BEGIN", self.meta_info)

                    curr_message += " " + transcript

                if msg["speech_final"] and self.callee_speaking:
                    yield create_ws_data_packet(curr_message, self.meta_info)
                    curr_message = ""
                    yield create_ws_data_packet("TRANSCRIBER_END", self.meta_info)
                    self.callee_speaking = False
                    self.last_vocal_frame_time = None
                    self.previous_request_id = self.current_request_id
                    self.current_request_id = None
            except Exception as e:
                logger.error(f"Error while getting transcriptions {e}")
                yield create_ws_data_packet("TRANSCRIBER_END", self.meta_info)

    def deepgram_connect(self):
        websocket_url = self.get_deepgram_ws_url()
        extra_headers = {
            'Authorization': 'Token {}'.format(os.getenv('DEEPGRAM_AUTH_TOKEN'))
        }
        deepgram_ws = websockets.connect(websocket_url, extra_headers=extra_headers)

        return deepgram_ws

    async def transcribe(self):
        async with self.deepgram_connect() as deepgram_ws:
            sender_task = asyncio.create_task(self.sender(deepgram_ws))
            heartbeat_task = asyncio.create_task(self.send_heartbeat(deepgram_ws))

            async for message in self.receiver(deepgram_ws):
                if self.connection_on:
                    yield message
                else:
                    logger.info("Closing the connection")
                    await self._close(deepgram_ws, data={"type": "CloseStream"})