// Overwritten by scripts/deploy.* on every deploy. Leave rum values empty to disable Datadog RUM.
window.AVIONPULSE_CONFIG = {
  env: 'local',
  version: '1.0.0',
  rum: {
    applicationId: '',
    clientToken: '',
    site: 'datadoghq.com',
    scriptUrl: 'https://www.datadoghq-browser-agent.com/us1/v6/datadog-rum.js',
    replaySampleRate: 20,
  },
};
