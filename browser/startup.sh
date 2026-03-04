#!/bin/bash
set -e

# Track child PIDs for graceful shutdown
VNC_PID=""
NOVNC_PID=""
APP_PID=""

# Graceful shutdown handler
shutdown() {
  echo "Received shutdown signal, stopping services..."
  if [ -n "$APP_PID" ] && kill -0 $APP_PID 2>/dev/null; then
    echo "Stopping launch.py (PID $APP_PID)..."
    kill -TERM $APP_PID 2>/dev/null || true
    wait $APP_PID 2>/dev/null || true
  fi
  if [ -n "$NOVNC_PID" ] && kill -0 $NOVNC_PID 2>/dev/null; then
    echo "Stopping NoVNC (PID $NOVNC_PID)..."
    kill -TERM $NOVNC_PID 2>/dev/null || true
  fi
  echo "Stopping VNC server..."
  vncserver -kill $DISPLAY 2>/dev/null || true
  echo "Shutdown complete"
  exit 0
}

# Trap SIGTERM and SIGINT
trap shutdown SIGTERM SIGINT

# Set up VNC password and xstartup
mkdir -p ~/.vnc
echo "#!/bin/bash" > ~/.vnc/xstartup
echo "xrdb \$HOME/.Xresources 2>/dev/null || true" >> ~/.vnc/xstartup
echo "startxfce4 &" >> ~/.vnc/xstartup
echo "tail -f /dev/null" >> ~/.vnc/xstartup
chmod +x ~/.vnc/xstartup

# Remove old VNC locks
rm -rf /tmp/.X1-lock /tmp/.X11-unix/X1 ~/.vnc/*.log ~/.vnc/*.pid

# Start VNC server
echo "Starting VNC server on $DISPLAY..."
vncserver $DISPLAY -geometry $RESOLUTION -depth 24 -SecurityTypes None > /dev/null 2>&1

# Start NoVNC
echo "Starting NoVNC..."
websockify --web /usr/share/novnc/ $NOVNC_PORT localhost:$VNC_PORT > /dev/null 2>&1 &
NOVNC_PID=$!

# Wait for X11 display to be ready (up to 80 seconds)
echo "Waiting for display $DISPLAY to be ready..."
for i in $(seq 1 80); do
  if xdpyinfo -display $DISPLAY >/dev/null 2>&1; then
    echo "Display $DISPLAY is ready!"
    break
  fi
  sleep 1
done

# Start launch.py with logs to stdout (visible in docker logs)
echo "Starting launch.py..."
cd /home/headless/Desktop/app && /bin/uv run launch.py 2>&1 &
APP_PID=$!
echo "launch.py started (PID $APP_PID)"

# Wait for any child to exit (keeps container running)
wait -n 2>/dev/null || wait
