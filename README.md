# idevicerestore-docker-gui
A curses-based TUI wrapper for idevicerestore Docker workflows, with live logs, firmware selection, progress display, and host usbmuxd handling.

## Install and use
### Prerequisites:
- debian based OS
- installed Docker Environment
- Python 3

1. clone idevicerestore
  ```shell
  git clone https://github.com/libimobiledevice/idevicerestore.git
  ```

2. build the container
  ```shell
  cd idevicerestore/docker
  sudo ./build.sh
  ```

3. copy the script
  ```shell
  wget https://github.com/Alfried-Krupp-Schulmedienzentrum/idevicerestore-docker-gui/raw/refs/heads/main/restore_gui.py
  ```

4. make it executable
  ```shell
  chmod +x restore_gui.py
  ```

5. start it
  ```shell
  sudo ./restore_gui.py
  ```
