/*
 * Waveception bridge for a SecurOS Node.js Script object.
 *
 * It listens to every HTTP Event Gate object on this SecurOS computer. A
 * Waveception event with a mapped camera ID becomes a camera-owned VCA_EVENT,
 * starts recording with pre-alarm video, and stops recording after the
 * requested post-roll interval.
 */

'use strict';

const securos = require('securos');

securos.connect(async function (core) {
    const stopTimers = new Map();

    function numberValue(value, fallback) {
        const parsed = Number(value);
        return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback;
    }

    function textValue(value, fallback) {
        const text = String(value === undefined || value === null ? '' : value)
            .replace(/\+/g, ' ').trim();
        return text || fallback;
    }

    function rollbackTime(timestamp, preRollMs) {
        const parsed = timestamp ? new Date(timestamp) : new Date();
        const eventTime = Number.isNaN(parsed.getTime()) ? new Date() : parsed;
        eventTime.setTime(eventTime.getTime() - preRollMs);
        const pad = (value, width) => String(value).padStart(width, '0');
        return `${pad(eventTime.getHours(), 2)}:${pad(eventTime.getMinutes(), 2)}:` +
            `${pad(eventTime.getSeconds(), 2)}.${pad(eventTime.getMilliseconds(), 3)}`;
    }

    async function handleWaveceptionEvent(message) {
        const params = message.params || {};
        if (String(params.source || '').toLowerCase() !== 'waveception') {
            return;
        }

        const cameraId = String(params.camera_id || '').trim();
        if (!cameraId) {
            console.log(`Waveception event ${params.inception_event_id || ''} has no mapped camera.`);
            return;
        }

        const camera = await core.getObject('CAM', cameraId);
        if (!camera) {
            console.error(`Waveception camera ID ${cameraId} does not exist in SecurOS.`);
            return;
        }

        const preRollMs = numberValue(params.pre_roll_ms, 5000);
        const postRollMs = numberValue(params.post_roll_ms, 5000);
        const mediaClientId = textValue(params.media_client_id, '');
        const accessResult = textValue(params.access_result, 'unknown');
        const user = textValue(params.user, 'Unnamed user');
        const door = textValue(params.door, 'Unknown door');
        const timestamp = String(params.timestamp || new Date().toISOString());
        const description = `Access ${accessResult}: ${user} at ${door}`;

        // Create a camera-owned event so Event Viewer can associate the access
        // record with this camera and its archive timestamp.
        core.sendEvent('CAM', cameraId, 'VCA_EVENT', {
            plugin: 'Waveception',
            type: `access_${accessResult}`,
            description: description,
            comment: description,
            user: user,
            door: door,
            access_result: accessResult,
            inception_event_id: String(params.inception_event_id || ''),
            inception_user_id: String(params.inception_user_id || ''),
            time_iso: timestamp
        });

        core.doReact('CAM', cameraId, 'REC_ROLLBACK', {
            rollback_time_abs: rollbackTime(timestamp, preRollMs),
            hot_rec_time: preRollMs + postRollMs,
            start_rec: 1,
            priority: 0
        });

        if (!mediaClientId) {
            console.error('Waveception cannot create a literal bookmark: media_client_id is blank.');
        } else {
            const mediaClient = await core.getObject('MEDIA_CLIENT', mediaClientId);
            if (!mediaClient) {
                console.error(`Waveception Media Client ID ${mediaClientId} does not exist.`);
            } else {
                // Bookmark creation is a documented Media Client operation. A
                // dedicated client prevents camera activation from changing an
                // operator's working display.
                core.doReact('MEDIA_CLIENT', mediaClientId, 'CAM_MODE', {
                    cams: cameraId,
                    mode: 'live'
                });
                core.doReact('MEDIA_CLIENT', mediaClientId, 'ACTIVATE_CAM', {cam: cameraId});
                setTimeout(function () {
                    core.doReact(
                        'MEDIA_CLIENT', mediaClientId, 'ADD_BOOKMARK_TO_ACTIVE_CAM', {}
                    );
                    console.log(
                        `Waveception bookmark requested through Media Client ${mediaClientId} ` +
                        `for camera ${cameraId}: ${description}`
                    );
                }, 500);
            }
        }

        if (stopTimers.has(cameraId)) {
            clearTimeout(stopTimers.get(cameraId));
        }
        stopTimers.set(cameraId, setTimeout(function () {
            core.doReact('CAM', cameraId, 'REC_STOP', {priority: 0});
            stopTimers.delete(cameraId);
            console.log(`Waveception recording completed for camera ${cameraId}: ${description}`);
        }, postRollMs));

        console.log(`Waveception recording started for camera ${cameraId}: ${description}`);
    }

    const gateIds = await core.getObjectsIds('HTTP_EVENT_PROXY');
    if (!gateIds.length) {
        console.error('Waveception bridge found no HTTP Event Gate objects.');
        return;
    }
    for (const gateId of gateIds) {
        core.registerEventHandler('HTTP_EVENT_PROXY', gateId, 'RECEIVED', function (message) {
            handleWaveceptionEvent(message).catch(function (error) {
                console.error(`Waveception event processing failed: ${error.stack || error}`);
            });
        });
        console.log(`Waveception bridge listening to HTTP Event Gate ${gateId}.`);
    }

    const mediaClientIds = await core.getObjectsIds('MEDIA_CLIENT');
    if (!mediaClientIds.length) {
        console.error('Waveception bridge found no Media Client objects.');
    } else {
        console.log('Available SecurOS Media Clients for literal bookmark creation:');
        for (const mediaClientId of mediaClientIds.sort()) {
            const mediaClient = await core.getObject('MEDIA_CLIENT', mediaClientId);
            console.log(`  Media Client ${mediaClientId}: ${mediaClient ? mediaClient.name : 'Unknown'}`);
        }
    }
});
