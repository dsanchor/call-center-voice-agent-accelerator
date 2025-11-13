"""Handles media streaming to Azure Voice Live API via WebSocket."""

import asyncio
import base64
import json
import logging
import os
import uuid
from typing import Any, Dict, Optional

from azure.identity.aio import DefaultAzureCredential
from websockets.asyncio.client import connect as ws_connect
from websockets.typing import Data

logger = logging.getLogger(__name__)


def _build_avatar_config() -> Dict[str, Any]:
    """Construye la configuración del avatar desde variables de entorno."""
    character = os.getenv("AZURE_VOICE_AVATAR_CHARACTER", "lisa")
    style = os.getenv("AZURE_VOICE_AVATAR_STYLE")  # Opcional
    video_width = int(os.getenv("AZURE_VOICE_AVATAR_WIDTH", "1280"))
    video_height = int(os.getenv("AZURE_VOICE_AVATAR_HEIGHT", "720"))
    bitrate = int(os.getenv("AZURE_VOICE_AVATAR_BITRATE", "2000000"))
    
    config: Dict[str, Any] = {
        "character": character,
        "customized": False,  # false para avatares preconfigurados de Azure
        "video": {
            "resolution": {"width": video_width, "height": video_height},
            "bitrate": bitrate
        },
    }
    
    # Estilo opcional (requerido para algunos personajes)
    if style:
        config["style"] = style
    
    # Configuración de servidores ICE para NAT traversal
    ice_urls = os.getenv("AZURE_VOICE_AVATAR_ICE_URLS")
    if ice_urls:
        config["ice_servers"] = [
            {"urls": [url.strip() for url in ice_urls.split(",") if url.strip()]}
        ]
    
    return config


def session_config():
    """Returns the default session configuration for Voice Live."""
    # Verificar si el avatar está habilitado
    avatar_enabled = os.getenv("AZURE_VOICE_AVATAR_ENABLED", "false").lower() == "true"
    
    modalities = ["text", "audio"]
    if avatar_enabled:
        modalities.extend(["avatar", "animation"])
    
    config = {
        "type": "session.update",
        "session": {
            "modalities": modalities,
            "turn_detection": {
                "type": "azure_semantic_vad",
                "threshold": 0.3,
                "prefix_padding_ms": 200,
                "silence_duration_ms": 200,
                "remove_filler_words": False,
                "end_of_utterance_detection": {
                    "model": "semantic_detection_v1",
                    "threshold": 0.01,
                    "timeout": 2,
                },
            },
            "input_audio_noise_reduction": {
                "type": "azure_deep_noise_suppression"
            },
            "input_audio_echo_cancellation": {
                "type": "server_echo_cancellation"
            },
            "voice": {
                "name": "es-ES-Ximena:DragonHDLatestNeural",
                "type": "azure-standard",
                "temperature": 0.8,
            },
        },
        "event_id": ""
    }
    
    # Añadir configuración del avatar si está habilitado
    if avatar_enabled:
        config["session"]["avatar"] = _build_avatar_config()
        config["session"]["animation"] = {
            "model_name": "default",
            "outputs": ["blendshapes", "viseme_id"]
        }
    
    return config


class ACSMediaHandler:
    """Manages audio streaming between client and Azure Voice Live API."""

    def __init__(self, config):
        self.endpoint = config["AZURE_VOICE_LIVE_ENDPOINT"]
        self.model = config["VOICE_LIVE_MODEL"]
        self.api_key = config["AZURE_VOICE_LIVE_API_KEY"]
        self.agent_project_name = config["AZURE_AGENT_PROJECT_NAME"]
        self.agent_id = config["AZURE_AGENT_ID"]
        # self.client_id = config["AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID"]
        self.send_queue = asyncio.Queue()
        self.ws = None
        self.send_task = None
        self.incoming_websocket = None
        self.is_raw_audio = True
        # Avatar support
        self._avatar_ice_servers: Optional[list] = None
        self._avatar_future: Optional[asyncio.Future] = None

    def _generate_guid(self):
        return str(uuid.uuid4())

    @staticmethod
    def _encode_client_sdp(client_sdp: str) -> str:
        """
        Codifica el SDP offer del cliente como JSON base64.
        Azure Voice Live espera: base64({"type": "offer", "sdp": "..."})
        """
        payload = json.dumps({"type": "offer", "sdp": client_sdp})
        return base64.b64encode(payload.encode("utf-8")).decode("ascii")

    @staticmethod
    def _decode_server_sdp(server_sdp_raw: Optional[str]) -> Optional[str]:
        """
        Decodifica el SDP answer de Azure Voice Live.
        Puede venir como SDP plano o como JSON base64.
        """
        if not server_sdp_raw:
            return None
        
        # Si ya es SDP plano (empieza con "v=0")
        if server_sdp_raw.startswith("v=0"):
            return server_sdp_raw
        
        try:
            # Decodificar base64
            decoded_bytes = base64.b64decode(server_sdp_raw)
            decoded_text = decoded_bytes.decode("utf-8")
            
            # Intentar parsear como JSON
            payload = json.loads(decoded_text)
            
            if isinstance(payload, dict):
                sdp_value = payload.get("sdp")
                if isinstance(sdp_value, str) and sdp_value:
                    return sdp_value
            
            # Si no es JSON válido, devolver el texto decodificado
            return decoded_text
        except Exception as exc:
            logger.warning("Error decoding server SDP: %s", exc)
            # En caso de error, devolver el valor original
            return server_sdp_raw

    async def connect_avatar(self, client_sdp: str) -> str:
        """
        Maneja la negociación WebRTC para conectar el avatar.
        
        Args:
            client_sdp: SDP offer generado por el cliente (frontend)
            
        Returns:
            server_sdp: SDP answer de Azure Voice Live
        """
        if not self.ws:
            raise RuntimeError("WebSocket not connected")
        
        # Crear future para esperar la respuesta asíncrona
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._avatar_future = future
        
        # Codificar el SDP offer del cliente
        encoded_sdp = self._encode_client_sdp(client_sdp)
        
        # Preparar payload para Azure Voice Live
        payload = {
            "client_sdp": encoded_sdp,
            "rtc_configuration": {
                "bundle_policy": "max-bundle"  # Optimización para WebRTC
            },
        }
        
        # Enviar evento session.avatar.connect
        await self._send_json({"type": "session.avatar.connect", **payload})
        
        try:
            # Esperar el SDP answer con timeout de 20 segundos
            server_sdp = await asyncio.wait_for(future, timeout=20)
            return server_sdp
        except asyncio.TimeoutError:
            logger.error("Timeout waiting for avatar SDP answer")
            raise RuntimeError("Avatar connection timeout")
        finally:
            self._avatar_future = None

    def get_avatar_ice_servers(self) -> Optional[list]:
        """Retorna los ICE servers capturados de la sesión."""
        return self._avatar_ice_servers

    async def connect(self):
        """Connects to Azure Voice Live API via WebSocket."""

        credential = DefaultAzureCredential()
        scopes = "https://ai.azure.com/.default"
        token = await credential.get_token(scopes)

        # log token
        logger.info(f"Using Azure AD token for {scopes}: {token.token}")

        url = f"{self.endpoint}/voice-live/realtime?api-version=2025-10-01&agent-project-name={self.agent_project_name}&agent-id={self.agent_id}&agent-access-token={token.token}"
        url = url.replace("https://", "wss://")

        headers = {"x-ms-client-request-id": self._generate_guid()}
        headers["Authorization"] = f"Bearer {token.token}"


        self.ws = await ws_connect(url, additional_headers=headers)
        logger.info("[VoiceLiveACSHandler] Connected to Voice Live API")

        await self._send_json(session_config())
        await self._send_json({"type": "response.create"})

        asyncio.create_task(self._receiver_loop())
        self.send_task = asyncio.create_task(self._sender_loop())

    async def init_incoming_websocket(self, socket, is_raw_audio=True):
        """Sets up incoming ACS WebSocket."""
        self.incoming_websocket = socket
        self.is_raw_audio = is_raw_audio

    async def audio_to_voicelive(self, audio_b64: str):
        """Queues audio data to be sent to Voice Live API."""
        await self.send_queue.put(
            json.dumps({"type": "input_audio_buffer.append", "audio": audio_b64})
        )

    async def _send_json(self, obj):
        """Sends a JSON object over WebSocket."""
        if self.ws:
            await self.ws.send(json.dumps(obj))

    async def _sender_loop(self):
        """Continuously sends messages from the queue to the Voice Live WebSocket."""
        try:
            while True:
                msg = await self.send_queue.get()
                if self.ws:
                    await self.ws.send(msg)
        except Exception:
            logger.exception("[VoiceLiveACSHandler] Sender loop error")

    async def _receiver_loop(self):
        """Handles incoming events from the Voice Live WebSocket."""
        try:
            async for message in self.ws:
                event = json.loads(message)
                event_type = event.get("type")

                match event_type:
                    case "session.created":
                        session_id = event.get("session", {}).get("id")
                        logger.info("[VoiceLiveACSHandler] Session ID: %s", session_id)

                    case "session.updated":
                        # Evento CRÍTICO para capturar ICE servers del avatar
                        session = event.get("session", {})
                        avatar = session.get("avatar", {})
                        
                        # Los ICE servers pueden venir en diferentes ubicaciones
                        candidate_sources = [
                            avatar.get("ice_servers"),
                            session.get("rtc", {}).get("ice_servers"),
                            session.get("ice_servers"),
                        ]
                        
                        ice_servers = next((s for s in candidate_sources if isinstance(s, list)), None)
                        
                        if ice_servers:
                            # Normalizar formato de ICE servers
                            normalized = []
                            for entry in ice_servers:
                                if isinstance(entry, str):
                                    normalized.append({"urls": entry})
                                elif isinstance(entry, dict) and "urls" in entry:
                                    normalized.append(entry)
                            
                            # Guardar para uso posterior
                            self._avatar_ice_servers = normalized
                            logger.info("[VoiceLiveACSHandler] Received %d ICE server(s)", len(normalized))
                        
                        # Enviar el evento completo al frontend
                        await self.send_message(
                            json.dumps({"Kind": "SessionUpdated", "Event": event})
                        )

                    case "session.avatar.connecting":
                        # Evento CRÍTICO con el SDP answer de Azure
                        server_sdp = event.get("server_sdp")
                        decoded_sdp = self._decode_server_sdp(server_sdp)
                        
                        # Resolver el future que espera connect_avatar()
                        if self._avatar_future and not self._avatar_future.done():
                            if decoded_sdp is None:
                                self._avatar_future.set_exception(
                                    RuntimeError("Empty server SDP")
                                )
                            else:
                                self._avatar_future.set_result(decoded_sdp)
                        
                        # Notificar al frontend
                        await self.send_message(
                            json.dumps({"Kind": "AvatarConnecting"})
                        )
                        logger.info("[VoiceLiveACSHandler] Avatar connecting")

                    case "input_audio_buffer.cleared":
                        logger.info("Input Audio Buffer Cleared Message")

                    case "input_audio_buffer.speech_started":
                        logger.info(
                            "Voice activity detection started at %s ms",
                            event.get("audio_start_ms"),
                        )
                        await self.stop_audio()

                    case "input_audio_buffer.speech_stopped":
                        logger.info("Speech stopped")

                    case "conversation.item.input_audio_transcription.completed":
                        transcript = event.get("transcript")
                        logger.info("User: %s", transcript)
                        await self.send_message(
                            json.dumps({"Kind": "UserTranscription", "Text": transcript})
                        )

                    case "conversation.item.input_audio_transcription.failed":
                        error_msg = event.get("error")
                        logger.warning("Transcription Error: %s", error_msg)

                    case "response.done":
                        response = event.get("response", {})
                        logger.info("Response Done: Id=%s", response.get("id"))
                        if response.get("status_details"):
                            logger.info(
                                "Status Details: %s",
                                json.dumps(response["status_details"], indent=2),
                            )

                    case "response.audio_transcript.done":
                        transcript = event.get("transcript")
                        logger.info("AI: %s", transcript)
                        await self.send_message(
                            json.dumps({"Kind": "AssistantTranscription", "Text": transcript})
                        )

                    case "response.audio.delta":
                        delta = event.get("delta")
                        if self.is_raw_audio:
                            audio_bytes = base64.b64decode(delta)
                            await self.send_message(audio_bytes)
                        else:
                            await self.voicelive_to_acs(delta)

                    case "error":
                        logger.error("Voice Live Error: %s", event)

                    case _:
                        logger.debug(
                            "[VoiceLiveACSHandler] Other event: %s", event_type
                        )
        except Exception:
            logger.exception("[VoiceLiveACSHandler] Receiver loop error")

    async def send_message(self, message: Data):
        """Sends data back to client WebSocket."""
        try:
            await self.incoming_websocket.send(message)
        except Exception:
            logger.exception("[VoiceLiveACSHandler] Failed to send message")

    async def voicelive_to_acs(self, base64_data):
        """Converts Voice Live audio delta to ACS audio message."""
        try:
            data = {
                "Kind": "AudioData",
                "AudioData": {"Data": base64_data},
                "StopAudio": None,
            }
            await self.send_message(json.dumps(data))
        except Exception:
            logger.exception("[VoiceLiveACSHandler] Error in voicelive_to_acs")

    async def stop_audio(self):
        """Sends a StopAudio signal to ACS."""
        stop_audio_data = {"Kind": "StopAudio", "AudioData": None, "StopAudio": {}}
        await self.send_message(json.dumps(stop_audio_data))

    async def acs_to_voicelive(self, stream_data):
        """Processes audio from ACS and forwards to Voice Live if not silent."""
        try:
            data = json.loads(stream_data)
            if data.get("kind") == "AudioData":
                audio_data = data.get("audioData", {})
                if not audio_data.get("silent", True):
                    await self.audio_to_voicelive(audio_data.get("data"))
        except Exception:
            logger.exception("[VoiceLiveACSHandler] Error processing ACS audio")

    async def web_to_voicelive(self, audio_bytes):
        """Encodes raw audio bytes and sends to Voice Live API."""
        audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
        await self.audio_to_voicelive(audio_b64)
