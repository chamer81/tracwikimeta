#!/bin/sh

svn update .
python setup.py bdist_egg
cp dist/*.egg /home/srv/trac/plugins/
systemctl restart tracd
