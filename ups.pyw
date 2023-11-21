from pprint import pprint
import socket
import time
import threading
import tkinter as tk
import tkinter.ttk

def hms(seconds):
	t = seconds;	h = t // 3600
	t = t % 3600;	m = t // 60
	t = t % 60;	s = t
	return "%dh %dm" % (h, m)

class NutUps:
	PORT = 3493
	
	def __init__(self, hostname, upsname):
		self.hostname = hostname
		self.upsname = upsname
		self.sock = None
		self.stream = None

	def connect(self):
		print("connecting", self.hostname)
		res = socket.getaddrinfo(self.hostname,
					 self.PORT,
					 type=socket.SOCK_STREAM)
		for (af, kind, proto, cname, addr) in res:
			self.sock = socket.socket(af, kind, proto)
			self.sock.connect(addr)
			self.stream = self.sock.makefile("rw")
			break

	def tryconnect(self):
		if not self.sock:
			self.connect()

	def close(self):
		self.stream.close()
		self.sock.close()

	def send(self, line):
		self.tryconnect()
		self.stream.write(line + "\n")
		self.stream.flush()

	def recv(self):
		line = self.stream.readline()
		if line:
			return line.rstrip("\r\n")
		else:
			return None

	@staticmethod
	def tokenize(line):
		import shlex
		return shlex.split(line)

	def recvone(self):
		line = self.recv()
		if line is None:
			raise IOError("Protocol error: End of stream")
		elif not line:
			raise IOError("Protocol error: Empty line")
		words = self.tokenize(line)
		return words

	def recvlist(self):
		items = []
		topic = None
		while True:
			words = self.recvone()
			if words[0:2] == ["BEGIN", "LIST"]:
				if topic is not None:
					raise IOError("Protocol error: Unexpected BEGIN in the middle of a list: %r" % (words,))
				topic = words[2]
			elif words[0:2] == ["END", "LIST"]:
				if topic is None:
					raise IOError("Protocol error: Unexpected END without BEGIN: %r" % (words,))
				break
			elif words[0] == topic:
				items.append(tuple(words[1:]))
			else:
				raise IOError("Protocol error: Unexpected line: %r" % (words,))
		return items

	def listvars(self):
		self.send("LIST VAR %s" % self.upsname)
		resp = self.recvlist()
		vars = {}
		for ups, var, value in resp:
			vars[var] = value
		return vars
	
	def getvar(self, name):
		self.tryconnect()
		(val,) = self.getvars(name)
		return val
	
	def getvars(self, *names):
		self.tryconnect()
		values = []
		for name in names:
			head = ("VAR", self.upsname, name)
			self.send("GET %s %s %s" % head)
		for name in names:
			head = ("VAR", self.upsname, name)
			resp = self.recvone()
			if tuple(resp[:2]) == ("ERR", "VAR-NOT-SUPPORTED"):
				values.append(None)
			elif tuple(resp[:len(head)]) != head:
				raise IOError("Protocol error: Unexpected response: %r; expected: %r" % (resp, head))
			else:
				values.append(resp[3])
		return tuple(values)

host = "dust"
upsname = "apc"

servers = [
	("dust", "apc"),
	("ember", "rack"),
	("wind", "rack"),
]

root = tk.Tk()

class UpsInfoWidget:
	def __init__(self, parent):
		self.timer = None
		
		frame = tk.ttk.Frame(parent, padding=(10,5,10,5))
		frame.pack()

		tk.Label(frame, text="UPS:").grid(row=0, column=0, sticky=tk.E)
		tk.Label(frame, text="Battery:").grid(row=1, column=0, sticky=tk.E)
		tk.Label(frame, text="Load:").grid(row=2, column=0, sticky=tk.E)
		tk.Label(frame, text="Runtime:").grid(row=3, column=0, sticky=tk.E)
		tk.Label(frame, text="Power:").grid(row=4, column=0, sticky=tk.E)

		self.server_str = tk.Label(frame, justify=tk.LEFT)
		self.server_str.grid(row=0, column=1, columnspan=2, sticky=tk.W)

		self.batt_bar = tk.ttk.Progressbar(frame)
		self.batt_bar.grid(row=1, column=1)

		self.batt_str = tk.Label(frame)
		self.batt_str.grid(row=1, column=2, sticky=tk.E)

		self.load_bar = tk.ttk.Progressbar(frame)
		self.load_bar.grid(row=2, column=1)

		self.load_str = tk.Label(frame)
		self.load_str.grid(row=2, column=2, sticky=tk.E)

		self.runeta_str = tk.Label(frame)
		self.runeta_str.grid(row=3, column=1, columnspan=2, sticky=tk.W)

		self.power_str = tk.Label(frame)
		self.power_str.grid(row=4, column=1, columnspan=2, sticky=tk.W)

	def updateonce(self, ups):
		print("called for", ups.hostname)
		self.server_str["text"] = "%s on %s" % (ups.upsname, ups.hostname)

		batt, load, runeta = ups.getvars("battery.charge", "ups.load", "battery.runtime")
		
		self.batt_bar["value"] = int(float(batt))
		self.batt_str["text"] = "%s%%" % int(float(batt))
		
		self.load_bar["value"] = int(float(load))
		self.load_str["text"] = "%s%%" % int(float(load))
		
		self.runeta_str["text"] = hms(int(float(runeta)))
		
		# VA is apparent power, W is real power (identical in DC)
		# V * A => W
		# rms(V) * rms(A) => VA
		# VA * pf => W
		try:
			# realpower is in watts?
			maxpower = float(ups.getvar("ups.realpower.nominal"))
			curpower = maxpower * float(load) / 100
			self.power_str["text"] = "approx. %.0fW" % curpower
		except KeyError:
			# power is in VA, power*powerfactor is in watts
			# output.voltage * output.current == power.nominal * load * powerfactor
			nompower = ups.getvar("ups.power.nominal")
			pwrfactor = ups.getvar("output.powerfactor")
			curpower = float(nompower) * float(load) / 100 * float(pwrfactor)
			self.power_str["text"] = "approx. %.0fW" % curpower
			
			# outcurrent = float(ups.getvar("output.current"))
			# outvoltage = float(ups.getvar("output.voltage"))
			# realpower = outcurrent * outvoltage
		
	def updatetimer(self, ups):
		self.updateonce(ups)
		self.timer = root.after(interval, self.updatetimer, ups)
	
	def bgupdate(self, ups):
		self.thread = threading.Thread(target=self.updateonce, args=(ups,))
		self.thread.start()
		self.timer = root.after(interval, self.bgupdate, ups)

interval = 1*1000
for h, u in servers:
	ups = NutUps(h, u)
	ifr = UpsInfoWidget(root)
	#root.after(100, ifr.bgupdate, ups)
	root.after(100, ifr.updatetimer, ups)

#def pair2str(hostname, upsname):
#	return "%s on %s" % (upsname, hostname)
#def str2pair(string):
#	upsname, hostname = string.split(" on ")
#	return hostname, upsname
#def server_changed(new_value):
#	global ups
#	global infoframe
#	hostname, upsname = str2pair(new_value)
#	if infoframe.timer:
#		root.after_cancel(infoframe.timer)
#		infoframe.timer = None
#	if ups:
#		ups.close()
#	ups = NutUps(hostname, upsname)
#	infoframe.update(ups)
#ups = None
#frame = tk.ttk.Frame(root, padding=(5,5,5,0))
#frame.pack()
#server_var = tk.StringVar(frame, value="Select server")
#server_strs = [pair2str(h, u) for (h, u) in servers]
#server_menu = tk.OptionMenu(frame, server_var, *server_strs, command=server_changed)
#server_menu.pack()
#infoframe = UpsInfoWidget(root)

root.mainloop()
