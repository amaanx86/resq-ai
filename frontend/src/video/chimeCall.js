// Thin wrapper over amazon-chime-sdk-js 3.x for a two-party emergency call (VIDEO-1).
// Loaded lazily (dynamic import) so the portal bundle doesn't carry the Chime SDK.
import {
  ConsoleLogger,
  DefaultDeviceController,
  DefaultMeetingSession,
  LogLevel,
  MeetingSessionConfiguration,
  MeetingSessionStatusCode
} from 'amazon-chime-sdk-js';

import { FRONT_CAMERA } from './media';

/**
 * handlers: {
 *   getLocalVideoElement(), getRemoteVideoElement(),   // HTMLVideoElement | null
 *   getAudioElement(),                                 // HTMLAudioElement for remote audio
 *   onStarted(), onRemoteVideo(hasVideo), onRemotePresence(present, externalUserId),
 *   onEnded(reason, message), onError(message)
 * }
 */
export class ChimeCall {
  constructor({ meeting, attendee, handlers }) {
    this.handlers = handlers;
    this.logger = new ConsoleLogger('ResQVideo', LogLevel.WARN);
    this.deviceController = new DefaultDeviceController(this.logger);
    const configuration = new MeetingSessionConfiguration({ Meeting: meeting }, { Attendee: attendee });
    this.session = new DefaultMeetingSession(configuration, this.logger, this.deviceController);
    this.av = this.session.audioVideo;
    this.selfAttendeeId = attendee.AttendeeId;
    this.remoteTileId = null;
    this.left = false;
    this.videoOn = false;

    this.observer = {
      audioVideoDidStart: () => this.handlers.onStarted?.(),
      audioVideoDidStop: (status) => this._didStop(status),
      videoTileDidUpdate: (tile) => this._tileUpdated(tile),
      videoTileWasRemoved: (tileId) => {
        if (tileId === this.remoteTileId) {
          this.remoteTileId = null;
          this.handlers.onRemoteVideo?.(false);
        }
      }
    };
    this.presence = (attendeeId, present, externalUserId) => {
      if (attendeeId === this.selfAttendeeId) return;
      // Ignore content-share attendees ("<id>#content").
      if (attendeeId.includes('#content')) return;
      this.handlers.onRemotePresence?.(present, externalUserId);
    };
  }

  async start({ audio, video }) {
    this.av.addObserver(this.observer);
    this.av.realtimeSubscribeToAttendeeIdPresence(this.presence);

    if (audio) {
      try {
        const devices = await this.av.listAudioInputDevices();
        // Empty constraints = the browser's default microphone.
        await this.av.startAudioInput(devices[0]?.deviceId || {});
      } catch (e) {
        this.logger.warn(`audio input failed: ${e}`);
      }
    }
    const audioEl = this.handlers.getAudioElement?.();
    if (audioEl) await this.av.bindAudioElement(audioEl);

    this.av.start();
    if (video) {
      try {
        await this.setCamera(true);
      } catch (e) {
        this.logger.warn(`video input failed: ${e}`);
        this.videoOn = false;
      }
    }
  }

  _tileUpdated(tile) {
    if (!tile.boundAttendeeId || tile.isContent) return;
    if (tile.localTile) {
      const el = this.handlers.getLocalVideoElement?.();
      if (el) this.av.bindVideoElement(tile.tileId, el);
      return;
    }
    const el = this.handlers.getRemoteVideoElement?.();
    if (el) {
      this.av.bindVideoElement(tile.tileId, el);
      this.remoteTileId = tile.tileId;
      this.handlers.onRemoteVideo?.(Boolean(tile.active));
    }
  }

  _didStop(status) {
    const code = status.statusCode();
    if (code === MeetingSessionStatusCode.Left || this.left) {
      this.handlers.onEnded?.('left', 'You left the call.');
    } else if (code === MeetingSessionStatusCode.MeetingEnded) {
      this.handlers.onEnded?.('meeting_ended', 'The doctor ended this call.');
    } else if (code === MeetingSessionStatusCode.AudioJoinedFromAnotherDevice) {
      this.handlers.onEnded?.('other_device', 'You joined this call from another device or tab.');
    } else if (status.isTerminal()) {
      this.handlers.onError?.(`The call was disconnected (${MeetingSessionStatusCode[code] || code}).`);
    } else {
      this.handlers.onEnded?.('stopped', 'The call stopped.');
    }
  }

  setMuted(muted) {
    if (muted) this.av.realtimeMuteLocalAudio();
    else this.av.realtimeUnmuteLocalAudio();
  }

  async setCamera(on) {
    if (on) {
      await this.av.startVideoInput(FRONT_CAMERA);
      this.av.startLocalVideoTile();
      this.videoOn = true;
    } else {
      this.av.stopLocalVideoTile();
      await this.av.stopVideoInput();
      this.videoOn = false;
    }
  }

  async leave() {
    if (this.left) return;
    this.left = true;
    try {
      this.av.realtimeUnsubscribeToAttendeeIdPresence(this.presence);
      this.av.stopLocalVideoTile();
      this.av.stop();
      await this.av.stopVideoInput();
      await this.av.stopAudioInput();
      this.av.unbindAudioElement();
      await this.deviceController.destroy();
    } catch (e) {
      this.logger.warn(`leave cleanup: ${e}`);
    } finally {
      this.av.removeObserver(this.observer);
    }
  }
}
