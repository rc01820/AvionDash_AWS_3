// Local-mode config. Only scripts/local_server.py serves this file: on AWS the SiteDeployment
// custom resource writes its own config.js. Leave rum values empty to disable RUM.
window.AVIONPULSE_CONFIG = {
  env: 'local',
  version: '3.0.0',
  rum: {
    applicationId: '',
    clientToken: '',
    site: 'datadoghq.com',
    scriptUrl: 'https://www.datadoghq-browser-agent.com/us1/v6/datadog-rum.js',
    replaySampleRate: 20,
  },
};
