'use strict';
// Where everything is, in the two modes:
//
//   dev (npm run dev)   code from the repo; the backend runs with your own Python from the repo; your
//                       files stay where the web version keeps them (the repo folder), so the desktop
//                       app and the web version see the same data. Set JARVIS_HOME to use another folder.
//   packaged (installer) the frozen backend in resources/backend; templates in resources/templates;
//                       your files in %APPDATA%\Jarvis\home, which installing, updating or
//                       uninstalling never touches.
const path = require('path');

function resolvePaths({ packaged, resourcesPath, appData, env = process.env }) {
  const repo = path.resolve(__dirname, '..', '..');
  // The app's own state (desktop.log, the Chrome desk profile). JARVIS_APP_DIR: tests use a throwaway one.
  const appDir = env.JARVIS_APP_DIR || path.join(appData, 'Jarvis');
  if (packaged) {
    return {
      mode: 'packaged',
      repo: null,
      appDir,
      home: env.JARVIS_HOME || path.join(appDir, 'home'),
      templates: path.join(resourcesPath, 'templates'),
      backend: { command: path.join(resourcesPath, 'backend', 'jarvis-backend.exe'), args: [], cwd: path.join(resourcesPath, 'backend') },
    };
  }
  return {
    mode: 'dev',
    repo,
    appDir,
    home: env.JARVIS_HOME || repo,
    templates: repo,
    backend: { command: env.JARVIS_PYTHON || 'python', args: ['-u', path.join(repo, 'desktop', 'backend', 'run_backend.py')], cwd: repo },
  };
}

module.exports = { resolvePaths };
