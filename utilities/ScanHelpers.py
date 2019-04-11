import sys
from IPy import IP
from time import time
from tqdm import tqdm
from json import dumps
from os.path import join
from dns.query import xfr
from ipwhois import IPWhois
from dns.zone import from_xfr
from termcolor import colored
from dns.name import EmptyLabel
from dns.exception import DNSException
from sqlalchemy.exc import IntegrityError
from ssl import create_default_context, CERT_NONE
from concurrent.futures import ThreadPoolExecutor, as_completed
from socket import getaddrinfo, gethostbyaddr, socket, AF_INET, SOCK_STREAM
from dns.resolver import Resolver, NXDOMAIN, NoAnswer, NoNameservers, Timeout
from utilities.DatabaseHelpers import Wildcard, Resolution
import utilities.MiscHelpers


def zoneTransfer(domain, nameservers):
	print(colored("\n[*]-Attempting to zone transfer from the identified nameservers...", "yellow"))

	for nameserver in nameservers:
		try:
			zone = from_xfr(xfr(nameserver, domain))
			subdomains = set([str(key) for key in zone.nodes.keys()])

			print("  \__ {0}: {1}".format(colored("Unique subdomains retrieved:", "cyan"), colored(len(subdomains), "yellow")))
			return subdomains

		except Exception:
			continue

	print("  \__", colored("Failed to zone transfer.", "red"))
	return None


def getDNSrecords(domain, out_to_json):
	print(colored("[*]-Retrieving DNS Records...", "yellow"))

	RES = {}
	MX = []
	NS = []
	A = []
	AAAA = []
	SOA = []
	TXT = []

	resolver = Resolver()
	resolver.timeout = 1
	resolver.lifetime = 1

	rrtypes = ["A", "MX", "NS", "AAAA", "SOA", "TXT"]

	for r in rrtypes:
		try:
			Aanswer = resolver.query(domain, r)

			for answer in Aanswer:
				if r == "A":
					A.append(answer.address)
					RES.update({r: A})

				if r == "MX":
					MX.append(answer.exchange.to_text()[:-1])
					RES.update({r: MX})

				if r == "NS":
					NS.append(answer.target.to_text()[:-1])
					RES.update({r: NS})

				if r == "AAAA":
					AAAA.append(answer.address)
					RES.update({r: AAAA})

				if r == "SOA":
					SOA.append(answer.mname.to_text()[:-1])
					RES.update({r: SOA})

				if r == "TXT":
					TXT.append(str(answer))
					RES.update({r: TXT})

		except NXDOMAIN:
			pass

		except NoAnswer:
			pass

		except EmptyLabel:
			pass

		except NoNameservers:
			pass

		except Timeout:
			pass

		except DNSException:
			pass

	for key, value in RES.items():
		for record in value:
			print("  \__ {0}: {1}".format(colored(key, "cyan"), colored(record, "yellow")))

	if out_to_json:
		try:
			with open(join("results", domain, "dns.json"), "w") as dns_file:
				dns_file.write(dumps(RES))

		except OSError:
			pass

		except IOError:
			pass

	try:
		with open(join("results", domain, "dns.csv"), "w") as dns_file:
			for key, value in RES.items():
				for record in value:
					dns_file.write("{0}|{1}\n".format(key, record))

	except OSError:
		pass

	except IOError:
		pass

	return NS


def checkWildcard(timestamp, subdomain, domain):
	try:
		return (subdomain, [item[4][0] for item in getaddrinfo(".".join([timestamp, subdomain, domain]), None)])

	except Exception:
		return (subdomain, None)


def identifyWildcards(db, findings, domain, threads):
	sub_levels = utilities.MiscHelpers.uniqueSubdomainLevels(findings)
	timestamp = int(time())
	wildcards = set()
	numberOfChunks = 1
	leaveFlag = False

	if len(sub_levels) <= 100000:
		print(colored("\n[*]-Checking for wildcards...", "yellow"))

	else:
		print(colored("\n[*]-Checking for wildcards, in chunks of 100,000...", "yellow"))
		numberOfChunks = len(sub_levels) // 100000 + 1

	subLevelChunks = utilities.MiscHelpers.chunkify(sub_levels, 100000)
	iteration = 1

	for subLevelChunk in subLevelChunks:
		with ThreadPoolExecutor(max_workers=threads) as executor:
			tasks = {executor.submit(checkWildcard, str(timestamp), sub_level, domain) for sub_level in subLevelChunk}

			try:
				completed = as_completed(tasks)

				if iteration == numberOfChunks:
					leaveFlag = True

				if numberOfChunks == 1:
					completed = tqdm(completed, total=len(subLevelChunk), desc="  \__ {0}".format(colored("Progress", "cyan")), dynamic_ncols=True, leave=leaveFlag)

				else:
					completed = tqdm(completed, total=len(subLevelChunk), desc="  \__ {0}".format(colored("Progress {0}/{1}".format(iteration, len(subLevelChunks)), "cyan")), dynamic_ncols=True, leave=leaveFlag)

				for task in completed:
					result = task.result()

					if result[1] is not None:
						for address in result[1]:
							wildcards.add((".".join([result[0], domain]), address))

			except KeyboardInterrupt:
				completed.close()
				print(colored("\n[*]-Received keyboard interrupt! Shutting down...\n", "red"))
				executor.shutdown(wait=False)
				exit(-1)

		if iteration < numberOfChunks:
			sys.stderr.write("\033[F")

		iteration += 1

	optimized_wildcards = {}

	if wildcards:
		reversed_wildcards = [(".".join(reversed(hostname.split("."))), ip) for hostname, ip in wildcards]
		sorted_wildcards = sorted(reversed_wildcards, key=lambda rw: rw[0])

		for reversed_hostname, ip in sorted_wildcards:
			hostname = ".".join(reversed(reversed_hostname.split(".")))
			new_wildcard = True

			if ip in optimized_wildcards:
				for entry in optimized_wildcards[ip]:
					if len(hostname.split(".")) > len(entry.split(".")):
						if entry in hostname:
							new_wildcard = False

				if new_wildcard:
					optimized_wildcards[ip].append(hostname)

			else:
				optimized_wildcards[ip] = [hostname]

		for address, hostnames in list(optimized_wildcards.items()):
			for hostname in hostnames:
				db.add(Wildcard(subdomain=hostname.split(domain)[0][:-1], domain=domain, address=address, timestamp=timestamp))

				try:
					db.commit()

				except IntegrityError:
					db.rollback()

		new_wildcards = db.query(Wildcard).filter(Wildcard.domain == domain, Wildcard.timestamp == timestamp)
		print("    \__ {0} {1}".format(colored("Wildcards that were identified:", "yellow"), colored(new_wildcards.count(), "cyan")))

		for row in new_wildcards:
			print("      \__ {0}.{1} ==> {2}".format(colored("*", "red"), colored(".".join([row.subdomain, row.domain]), "cyan"), colored(row.address, "red")))


def resolve(finding, domain):
	try:
		return (finding[0], [item[4][0] for item in getaddrinfo(".".join([finding[0], domain]), None)], finding[1])

	except Exception:
		return (finding[0], None, finding[1])


def massResolve(db, findings, domain, threads):
	resolved = set()
	wildcards = {}
	timestamp = int(time())
	numberOfChunks = 1
	leaveFlag = False

	for row in db.query(Wildcard).filter(Wildcard.domain == domain):
		if row.subdomain in wildcards:
			wildcards[row.subdomain].append(row.address)

		else:
			wildcards[row.subdomain] = []
			wildcards[row.subdomain].append(row.address)

	if len(findings) <= 100000:
		print("{0} {1} {2}".format(colored("\n[*]-Attempting to resolve", "yellow"), colored(len(findings), "cyan"), colored("hostnames...", "yellow")))

	else:
		print("{0} {1} {2}".format(colored("\n[*]-Attempting to resolve", "yellow"), colored(len(findings), "cyan"), colored("hostnames, in chunks of 100,000...", "yellow")))
		numberOfChunks = len(findings) // 100000 + 1

	findingsChunks = utilities.MiscHelpers.chunkify(findings, 100000)
	iteration = 1

	for findingsChunk in findingsChunks:
		with ThreadPoolExecutor(max_workers=threads) as executor:
			tasks = {executor.submit(resolve, finding, domain) for finding in findingsChunk}

			try:
				completed = as_completed(tasks)

				if iteration == numberOfChunks:
					leaveFlag = True

				if numberOfChunks == 1:
					completed = tqdm(completed, total=len(findingsChunk), desc="  \__ {0}".format(colored("Progress", "cyan")), dynamic_ncols=True, leave=leaveFlag)

				else:
					completed = tqdm(completed, total=len(findingsChunk), desc="  \__ {0}".format(colored("Progress {0}/{1}".format(iteration, numberOfChunks), "cyan")), dynamic_ncols=True, leave=leaveFlag)

				for task in completed:
					try:
						result = task.result()

						if result[1] is not None:
							for address in result[1]:
								resolved.add((".".join([result[0], domain]), address, result[2]))

					except Exception:
						continue

			except KeyboardInterrupt:
				completed.close()
				print(colored("\n[*]-Received keyboard interrupt! Shutting down...\n", "red"))
				executor.shutdown(wait=False)
				exit(-1)

		if iteration < numberOfChunks:
			sys.stderr.write("\033[F")

		iteration += 1

	"""
	print("    \__ {0} {1}".format(colored("Hostnames that were resolved:", "yellow"), colored(len(resolved_diff), "cyan")))

	for hostname, address in list(resolved_diff.items()):
		if hostname not in already_resolved:
			if address in wildcards:
				actual_wildcard = False

				for value in wildcards[address]:
					if value in hostname:
						actual_wildcard = True

				if actual_wildcard:
					print("      \__ {0} ({1})".format(colored(hostname, "cyan"), colored(address, "red")))

				else:
					print("      \__ {0} ({1})".format(colored(hostname, "cyan"), colored(address, "yellow")))

			else:
				print("      \__ {0} ({1})".format(colored(hostname, "cyan"), colored(address, "yellow")))
	"""

def reverseLookup(IP):
	try:
		return (gethostbyaddr(IP)[0].lower(), IP)

	except Exception:
		return None


def massReverseLookup(IPs, threads):
	hosts = []
	leaveFlag = False

	if len(IPs) <= 100000:
		print("{0} {1} {2}".format(colored("\n[*]-Performing reverse DNS lookups on", "yellow"), colored(len(IPs), "cyan"), colored("unique public IPs...", "yellow")))
	else:
		print("{0} {1} {2}".format(colored("\n[*]-Performing reverse DNS lookups on", "yellow"), colored(len(IPs), "cyan"), colored("unique public IPs, in chunks of 100,000...", "yellow")))

	IPChunks = list(utilities.MiscHelpers.chunks(list(IPs), 100000))
	iteration = 1

	for IPChunk in IPChunks:
		with ThreadPoolExecutor(max_workers=threads) as executor:
			tasks = {executor.submit(reverseLookup, IP) for IP in IPChunk}

			try:
				completed = as_completed(tasks)

				if iteration == len(IPChunks):
					leaveFlag = True

				completed = tqdm(completed, total=len(IPChunk), desc="  \__ {0}".format(colored("Progress {0}/{1}".format(iteration, len(IPChunks)), "cyan")), dynamic_ncols=True, leave=leaveFlag)

				for task in completed:
					result = task.result()

					if result is not None:
						hosts.append(result)

			except KeyboardInterrupt:
				completed.close()
				print(colored("\n[*]-Received keyboard interrupt! Shutting down...\n", "red"))
				executor.shutdown(wait=False)
				exit(-1)

		if iteration < len(IPChunks):
			sys.stderr.write("\033[F")

		iteration += 1

	return hosts


def connectScan(target):
	isOpen = False

	try:
		s = socket(AF_INET, SOCK_STREAM)
		s.settimeout(1)
		result1 = s.connect_ex(target)

		if not result1:
			if target[1] != 80 and target[1] != 443:
				isOpen = True
				context = create_default_context()
				context.check_hostname = False
				context.verify_mode = CERT_NONE
				context.wrap_socket(s)

				return (target[0], target[1], True)

			elif target[1] == 80:
				return (target[0], target[1], False)

			elif target[1] == 443:
				return (target[0], target[1], True)

	except Exception as e:
		if isOpen:
			if "unsupported protocol" in str(e):
				return (target[0], target[1], True)

			else:
				return (target[0], target[1], False)

		else:
			return None

	finally:
		s.close()


def massConnectScan(IPs, targets, threads):
	open_ports = []
	leaveFlag = False

	if len(targets) <= 100000:
		print("{0} {1} {2} {3} {4}".format(colored("\n[*]-Scanning", "yellow"), colored(len(targets), "cyan"), colored("ports on", "yellow"), colored(len(IPs), "cyan"), colored("unique public IPs...", "yellow")))
	else:
		print("{0} {1} {2} {3} {4}".format(colored("\n[*]-Scanning", "yellow"), colored(len(targets), "cyan"), colored("ports on", "yellow"), colored(len(IPs), "cyan"), colored("unique public IPs, in chunks of 100,000...", "yellow")))

	PortChunks = list(utilities.MiscHelpers.chunks(list(targets), 100000))
	iteration = 1

	for PortChunk in PortChunks:
		with ThreadPoolExecutor(max_workers=threads) as executor:
			tasks = {executor.submit(connectScan, target) for target in PortChunk}

			try:
				completed = as_completed(tasks)

				if iteration == len(PortChunks):
					leaveFlag = True

				completed = tqdm(completed, total=len(PortChunk), desc="  \__ {0}".format(colored("Progress {0}/{1}".format(iteration, len(PortChunks)), "cyan")), dynamic_ncols=True, leave=leaveFlag)

				for task in completed:
					result = task.result()

					if result is not None:
						open_ports.append(result)

			except KeyboardInterrupt:
				completed.close()
				print(colored("\n[*]-Received keyboard interrupt! Shutting down...\n", "red"))
				executor.shutdown(wait=False)
				exit(-1)

		if iteration < len(PortChunks):
			sys.stderr.write("\033[F")

		iteration += 1

	return open_ports


def rdap(ip):
	try:
		obj = IPWhois(ip)
		result = obj.lookup_rdap()

		return result

	except Exception:
		return None


def massRDAP(domain, IPs, threads, out_to_json):
	rdap_records = []
	leaveFlag = False

	if len(IPs) <= 100000:
		print("{0} {1} {2}".format(colored("\n[*]-Performing RDAP lookups for", "yellow"), colored(len(IPs), "cyan"), colored("unique public IPs...", "yellow")))
	else:
		print("{0} {1} {2}".format(colored("\n[*]-Performing RDAP lookups for", "yellow"), colored(len(IPs), "cyan"), colored("unique public IPs, in chunks of 100,000...", "yellow")))

	IPChunks = list(utilities.MiscHelpers.chunks(list(IPs), 100000))
	iteration = 1

	for IPChunk in IPChunks:
		with ThreadPoolExecutor(max_workers=threads) as executor:
			tasks = {executor.submit(rdap, ip): ip for ip in IPChunk}

			try:
				completed = as_completed(tasks)

				if iteration == len(IPChunks):
					leaveFlag = True

				completed = tqdm(completed, total=len(IPs), desc="  \__ {0}".format(colored("Progress", "cyan")), dynamic_ncols=True, leave=leaveFlag)

				for task in completed:
					result = task.result()

					if result is not None:
						rdap_records.append(result)

			except KeyboardInterrupt:
				completed.close()
				print(colored("\n[*]-Received keyboard interrupt! Shutting down...\n", "red"))
				executor.shutdown(wait=False)
				exit(-1)

		if iteration < len(IPChunks):
			sys.stderr.write("\033[F")

		iteration += 1

	ASN = set()
	NETS = set()

	for record in rdap_records:
		if record["asn"] != "NA" and record["asn_cidr"] != "NA" and record["asn_description"] != "NA":
			for asn in record["asn"].split(" "):
				ASN.add((asn, record["asn_cidr"], record["asn_description"]))

		for cidr in record["network"]["cidr"].split(", "):
			NETS.add((cidr, record["network"]["name"]))

	print("    \__ {0}:".format(colored("Autonomous Systems that were identified", "yellow")))
	ASN = sorted(ASN, key=lambda k: int(k[0]))

	for asn in ASN:
		if asn == ASN[-1]:
			print("    __\__ {0}: {1}, {2}: {3}, {4}: {5}".format(colored("ASN", "cyan"), colored(asn[0], "yellow"), colored("Prefix", "cyan"), colored(asn[1], "yellow"), colored("Description", "cyan"), colored(asn[2], "yellow")))
			print("   \\")

		else:
			print("      \__ {0}: {1}, {2}: {3}, {4}: {5}".format(colored("ASN", "cyan"), colored(asn[0], "yellow"), colored("Prefix", "cyan"), colored(asn[1], "yellow"), colored("Description", "cyan"), colored(asn[2], "yellow")))

	print("    \__ {0}:".format(colored("Networks that were identified", "yellow")))
	NETS = sorted(NETS, key=lambda k: k[0])

	for net in NETS:
		print("      \__ {0}: {1}, {2}: {3}".format(colored("CIDR", "cyan"), colored(net[0], "yellow"), colored("Identifier", "cyan"), colored(net[1], "yellow")))

	if out_to_json:
		ASN_json = {}
		NETS_json = {}

		for asn in ASN:
			if asn[0] in ASN_json:
				ASN_json[asn[0]].append((asn[1], asn[2]))

			else:
				ASN_json[asn[0]] = [(asn[1], asn[2])]

		try:
			with open(join("results", domain, "asn.json"), "w") as asn_file:
				asn_file.write(dumps(ASN_json))

		except OSError:
			pass

		except IOError:
			pass

		for net in NETS:
			NETS_json[net[0]] = net[1]

		try:
			with open(join("results", domain, "networks.json"), "w") as net_file:
				net_file.write(dumps(NETS_json))

		except OSError:
			pass

		except IOError:
			pass

	try:
		with open(join("results", domain, "asn.csv"), "w") as asn_file:
			for asn in ASN:
				asn_file.write("{0}|{1}|{2}\n".format(asn[0], asn[1], asn[2]))

	except OSError:
		pass

	except IOError:
		pass

	try:
		with open(join("results", domain, "networks.csv"), "w") as net_file:
			for net in NETS:
				net_file.write("{0}|{1}\n".format(net[0], net[1]))

	except OSError:
		pass

	except IOError:
		pass
