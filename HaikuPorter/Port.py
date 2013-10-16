# -*- coding: utf-8 -*-
#
# Copyright 2007-2011 Brecht Machiels
# Copyright 2009-2010 Chris Roberts
# Copyright 2009-2011 Scott McCreary
# Copyright 2009 Alexander Deynichenko
# Copyright 2009 HaikuBot (aka RISC)
# Copyright 2010-2011 Jack Laxson (Jrabbit)
# Copyright 2011 Ingo Weinhold
# Copyright 2013 Oliver Tappe
# Distributed under the terms of the MIT License.

# -- Modules ------------------------------------------------------------------

from HaikuPorter.BuildPlatform import buildPlatform
from HaikuPorter.ConfigParser import ConfigParser
from HaikuPorter.Configuration import Configuration
from HaikuPorter.Options import getOption
from HaikuPorter.Package import (PackageType, sourcePackageFactory,
								 packageFactory)
from HaikuPorter.RecipeAttributes import getRecipeAttributes
from HaikuPorter.RecipeTypes import (Extendable, MachineArchitecture, Phase,
									 Status)
from HaikuPorter.RequiresUpdater import RequiresUpdater
from HaikuPorter.ShellScriptlets import (cleanupChrootScript,
										 getShellVariableSetters,
										 recipeActionScript,
										 setupChrootScript)
from HaikuPorter.Source import Source
from HaikuPorter.Utils import (filteredEnvironment, naturalCompare,
							   storeStringInFile, symlinkGlob, sysExit, 
							   touchFile, warn)

import os
import shutil
import signal
from subprocess import check_call, CalledProcessError
import traceback


# -- Modules preloaded for chroot ---------------------------------------------
# These modules need to be preloaded in order to avoid problems with python
# trying to dynamically load them inside a chroot environment
from encodings import string_escape


# -- Scoped resource for chroot environments ----------------------------------
class ChrootSetup(object):
	def __init__(self, chrootPath, envVars):
		self.path = chrootPath
		self.buildOk = False
		self.envVars = envVars

	def __enter__(self):
		# execute the chroot setup scriptlet via the shell ...
		os.chdir(self.path)
		shellEnv = filteredEnvironment()
		shellEnv.update(self.envVars)
		check_call(['/bin/bash', '-c', setupChrootScript], env=shellEnv)
		return self

	def __exit__(self, ignoredType, value, traceback):
		# execute the chroot cleanup scriptlet via the shell ...
		os.chdir(self.path)
		shellEnv = filteredEnvironment()
		shellEnv.update(self.envVars)
		if self.buildOk:
			shellEnv['buildOk'] = '1'
		check_call(['/bin/bash', '-c', cleanupChrootScript], env=shellEnv)


# -- A single port with its recipe, allows to execute actions -----------------
class Port(object):
	def __init__(self, name, version, category, baseDir, outputDir,
				 globalShellVariables, policy, secondaryArchitecture = None):
		self.baseName = name
		self.secondaryArchitecture = secondaryArchitecture
		self.name = name
		if secondaryArchitecture:
			self.name += '_' + secondaryArchitecture
		self.version = version
		self.versionedName = self.name + '-' + version
		self.category = category
		self.baseDir = baseDir
		self.outputDir = outputDir
		self.recipeIsBroken = False
		self.recipeHasBeenParsed = False

		if secondaryArchitecture:
			self.workDir = (self.outputDir + '/work-' + secondaryArchitecture
				+ '-' + self.version)
			self.effectiveTargetArchitecture = self.secondaryArchitecture
		else:
			self.workDir = self.outputDir + '/work-' + self.version
			self.effectiveTargetArchitecture \
				= buildPlatform.getTargetArchitecture()

		self.isMetaPort = self.category == 'meta-ports'

		self.recipeFilePath = (self.baseDir + '/' + self.baseName + '-'
							   + self.version + '.recipe')

		self.packageInfoName = self.versionedName + '.PackageInfo'

		self.revision = None
		self.fullVersion = None
		self.revisionedName = None

		self.definedPhases = []

		# build dictionary of variables to inherit to shell
		self.shellVariables = {
			'portName': self.name,
			'portVersion': self.version,
			'portVersionedName': self.versionedName,
			'portBaseDir': self.baseDir,
		}
		self.shellVariables.update(globalShellVariables)
		self._updateShellVariables(True)

		self.buildArchitecture = self.shellVariables['buildArchitecture']
		self.targetArchitecture = self.shellVariables['targetArchitecture']
		if (Configuration.isCrossBuildRepository()
			and '_cross_' in self.name):
			# the cross-tools (binutils and gcc) need to run on the build
			# architecture, not the target architecture
			self.hostArchitecture = self.shellVariables['buildArchitecture']
		else:
			self.hostArchitecture = self.shellVariables['targetArchitecture']

		# Each port creates at least two packages: the base package (which will
		# share its name with the port), and a source package.
		# Additional packages can be declared in the recipe, too. All packages
		# that are considered stable on the current architecture will be
		# collected in self.packages.
		self.allPackages = []
		self.packages = []

		if self.isMetaPort:
			self.downloadDir = None
			self.patchesDir = None
			self.licensesDir = None
			self.additionalFilesDir = None
		else:
			# create full paths for the directories
			if Configuration.shallDownloadInPortDirectory():
				self.downloadDir = self.baseDir + '/download'
			else:
				self.downloadDir = self.outputDir + '/download'
			self.patchesDir = self.baseDir + '/patches'
			self.licensesDir = self.baseDir + '/licenses'
			self.additionalFilesDir = self.baseDir + '/additional-files'

		self.sourceBaseDir = self.workDir + '/sources'
		self.packageInfoDir = self.workDir + '/package-infos'
		self.buildPackageDir = self.workDir + '/build-packages'
		self.packagingBaseDir = self.workDir + '/packaging'
		self.hpkgDir = self.workDir + '/hpkgs'

		self.preparedRecipeFile = self.workDir + '/port.recipe'

		self.policy = policy
		self.requiresUpdater = None

	def __enter__(self):
		return self

	def __exit__(self, type, value, traceback):
		pass

	def parseRecipeFileIfNeeded(self):
		"""Make sure that the recipe has been parsed and silently parse it if
		   it hasn't"""
		self.parseRecipeFile(False)
		
	def parseRecipeFile(self, showWarnings):
		"""Parse the recipe-file of the specified port, unless already done"""
		
		if self.recipeHasBeenParsed:
			return
		try:
			self._parseRecipeFile(showWarnings)
		finally:
			self.recipeHasBeenParsed = True

	def validateRecipeFile(self, showWarnings = False):
		"""Validate the syntax and contents of the recipe file"""

		if not os.path.exists(self.recipeFilePath):
			sysExit(self.name + ' version ' + self.version + ' not found.')

		# copy the recipe file and prepare it for use
		if not os.path.exists(os.path.dirname(self.preparedRecipeFile)):
			os.makedirs(os.path.dirname(self.preparedRecipeFile))

		prepareRecipeCommand = [ '/bin/bash', '-c',
			'sed \'s,^\\(REVISION="[^"]*"\\),\\1; updateRevisionVariables ,\' '
				+ self.recipeFilePath + ' > ' + self.preparedRecipeFile]
		check_call(prepareRecipeCommand)

		# adjust recipe attributes for meta ports
		recipeAttributes = getRecipeAttributes()
		if self.isMetaPort:
			recipeAttributes['HOMEPAGE']['required'] = False
			recipeAttributes['SRC_URI']['required'] = False

		# parse the recipe file
		recipeConfig = ConfigParser(self.preparedRecipeFile, recipeAttributes,
							  		self.shellVariables)
		extensions = recipeConfig.getExtensions()
		self.definedPhases = recipeConfig.getDefinedPhases()

		if '' not in extensions:
			sysExit('No base package defined in (in %s)' % self.recipeFilePath)

		recipeKeysByExtension = {}

		# do some checks for each extension (i.e. package), starting with the
		# base entries (extension '')
		baseEntries = recipeConfig.getEntriesForExtension('')
		allPatches = []
		for extension in sorted(extensions):
			entries = recipeConfig.getEntriesForExtension(extension)
			recipeKeys = {}

			# check whether all required values are present
			for baseKey in recipeAttributes.keys():
				if extension:
					key = baseKey + '_' + extension
					# inherit any missing attribute from the respective base
					# value or set the default
					if key not in entries:
						attributes = recipeAttributes[baseKey]
						if attributes['extendable'] == Extendable.DEFAULT:
							recipeKeys[baseKey] = attributes['default']
						else:
							if ('suffix' in attributes
								and extension in attributes['suffix']):
								recipeKeys[baseKey] = (
									baseEntries[baseKey]
									+ attributes['suffix'][extension])
							else:
								recipeKeys[baseKey] = baseEntries[baseKey]
							continue
								# inherited values don't need to be checked
				else:
					key = baseKey

				if key not in entries:
					# complain about missing required values
					if recipeAttributes[baseKey]['required']:
						sysExit("Required value '%s' not present (in %s)"
								% (key, self.recipeFilePath))

					# set default value, as no other value has been provided
					entries[key] = recipeAttributes[baseKey]['default']

				# The summary must be a single line of text, preferably not
				# exceeding 70 characters in length
				if baseKey == 'SUMMARY':
					if '\n' in entries[key]:
						sysExit('%s must be a single line of text (%s).'
							% (key, self.recipeFilePath))
					if len(entries[key]) > 70 and showWarnings:
						warn('%s exceeds 70 chars (in %s)'
							 % (key, self.recipeFilePath))

				# Check for a valid license file
				if baseKey == 'LICENSE':
					if key in entries and entries[key]:
						fileList = []
						recipeLicense = entries[key]
						for item in recipeLicense:
							haikuLicenseList = fileList = os.listdir(
								buildPlatform.getLicensesDirectory())
							if (item not in fileList and self.licensesDir
								and os.path.exists(self.licensesDir)):
								fileList = []
								licenses = os.listdir(self.licensesDir)
								for filename in licenses:
									fileList.append(filename)
							if item not in fileList:
								haikuLicenseList.sort()
								sysExit('No match found for license ' + item
										+ '\nValid license filenames included '
										+ 'with Haiku are:\n'
										+ '\n'.join(haikuLicenseList))
					elif showWarnings:
						warn('No %s found (in %s)' % (key, self.recipeFilePath))

				if baseKey == 'COPYRIGHT':
					if key not in entries or not entries[key]:
						if showWarnings:
							warn('No %s found (in %s)'
								 % (key, self.recipeFilePath))

				if baseKey == 'PATCHES':
					# collect all referenced patches into a single list
					if key in entries and entries[key]:
						for index in entries[key].keys():
							allPatches += entries[key][index]

				# store extension-specific value under base key
				recipeKeys[baseKey] = entries[key]

			recipeKeysByExtension[extension] = recipeKeys

		# If a patch file exists for this port, warn if that patch file isn't
		# referenced in "PATCHES"
		if not self.isMetaPort:
			versionedBaseName = self.baseName + '-' + self.version
			for fileExtension in ['diff', 'patch', 'patchset']:
				suffixes = ['', '-' + self.effectiveTargetArchitecture]
				if self.effectiveTargetArchitecture \
					== MachineArchitecture.X86_GCC2:
					suffixes.append('-gcc2')
				else:
					suffixes.append('-gcc4')
				for suffix in suffixes:
					patchFileName = '%s%s.%s' % (versionedBaseName, suffix,
												 fileExtension)
					if (os.path.exists(self.patchesDir + '/' + patchFileName)
						and not patchFileName in allPatches):
							if showWarnings:
								warn('Patch file %s is not referenced in '
									 'PATCHES, so it will not be used'
									 % patchFileName)

		return recipeKeysByExtension

	def printDescription(self):
		"""Show port description"""

		self.parseRecipeFileIfNeeded()
		print '*' * 80
		print 'VERSION: %s' % self.versionedName
		print 'REVISION: %s' % self.revision
		print 'HOMEPAGE: %s' % self.recipeKeys['HOMEPAGE']
		for package in self.allPackages:
			print '-' * 80
			print 'PACKAGE: %s' % package.versionedName
			print 'SUMMARY: %s' % package.recipeKeys['SUMMARY']
			print('STATUS: %s'
				  % package.getStatusOnArchitecture(self.targetArchitecture))
			print 'ARCHITECTURE: %s' % package.architecture
		print '*' * 80

	def getStatusOnTargetArchitecture(self):
		"""Return the status of this port on the target architecture"""

		try:
			self.parseRecipeFileIfNeeded()

			# use the status of the base package as overall status of the port
			return self.allPackages[0].getStatusOnSecondaryArchitecture(
				self.targetArchitecture, self.secondaryArchitecture)
		except:
			return Status.UNSUPPORTED

	def isBuildableOnTargetArchitecture(self):
		"""Returns whether or not this port is buildable on the target
		   architecture"""
		status = self.getStatusOnTargetArchitecture()
		allowUntested = Configuration.shallAllowUntested()
		return (status == Status.STABLE
			or (status == Status.UNTESTED and allowUntested))

	def hasBrokenRecipe(self):
		"""Returns whether or not the recipe for this port is broken (i.e. it
		   can't be parsed or contains errors)"""
		if not hasattr(self, 'recipeKeys'):
			try:
				self.parseRecipeFile(False)
			except:
				pass
		return self.recipeIsBroken

	def writePackageInfosIntoRepository(self, repositoryPath):
		"""Write one PackageInfo-file per stable package into the repository"""

		self.parseRecipeFileIfNeeded()
		for package in self.packages:
			package.writePackageInfoIntoRepository(repositoryPath)

	def removePackageInfosFromRepository(self, repositoryPath):
		"""Remove all PackageInfo-files for this port from the repository"""

		self.parseRecipeFileIfNeeded()
		for package in self.packages:
			package.removePackageInfoFromRepository(repositoryPath)

	def generatePackageInfoFiles(self, requiresTypes, targetPath = None):
		"""Generates package info files with given types of requires."""

		self.parseRecipeFileIfNeeded()
		return self._generatePackageInfoFiles(requiresTypes, targetPath)

	def obsoletePackages(self, packagesPath):
		"""Moves all package-files into the 'obsolete' sub-directory"""

		self.parseRecipeFileIfNeeded()
		for package in self.packages:
			package.obsoletePackage(packagesPath)

	def getMainPackage(self):
		self.parseRecipeFileIfNeeded()
		if self.packages:
			return self.packages[0]
		return None

	def getSourcePackage(self):
		self.parseRecipeFileIfNeeded()
		for package in self.packages:
			if package.type == PackageType.SOURCE:
				return package
		return None

	def sourcePackageExists(self, packagesPath):
		"""Determines if the source package already exists"""

		self.parseRecipeFileIfNeeded()
		package = self.getSourcePackage()
		return package and os.path.exists(packagesPath + '/' + package.hpkgName)

	def resolveBuildDependencies(self, repositoryPath, packagesPath):
		"""Resolve any other ports (no matter if required or prerequired) that
		   need to be built before this one.
		   Any build requirements a port may have that can not be fulfilled from
		   within the haikuports tree will be raised as an error here.
		"""

		workRepositoryPath = self.workDir + '/repository'
		symlinkGlob(repositoryPath + '/*.PackageInfo', workRepositoryPath)

		requiresTypes = [ 'BUILD_REQUIRES' ]
		packageInfoFiles = self._generatePackageInfoFiles(requiresTypes,
											 			  workRepositoryPath)
		requiredPackages \
			= self._resolveDependencies(packageInfoFiles, [ packagesPath ],
										'required ports', 
										buildPlatform.isHaiku(),
										[ workRepositoryPath ])

		requiresTypes = [ 'BUILD_PREREQUIRES', 'SCRIPTLET_PREREQUIRES' ]
		packageInfoFiles = self._generatePackageInfoFiles(requiresTypes,
											 			  workRepositoryPath)
		prerequiredPackages \
			= self._resolveDependencies(packageInfoFiles, [ packagesPath],
										'prerequired ports', True,
										[ workRepositoryPath ])

		# return list of unique ports which need to be built before this one
		processedPackages = set()
		result = []
		for package in requiredPackages + prerequiredPackages:
			if package in processedPackages:
				continue
			processedPackages.add(package)
			if package.startswith(workRepositoryPath):
				result.append(package)
		return result

	def whyIsPortRequired(self, repositoryPath, packagesPath, requiredPort):
		"""Find out which package is pulling the given port in as a dependency
		   of this port."""

		workRepositoryPath = self.workDir + '/repository'
		symlinkGlob(repositoryPath + '/*.PackageInfo', workRepositoryPath)

		# drop package-infos for the required port, such that pkgman will
		# fail with an appropriate message
		requiredPort.removePackageInfosFromRepository(workRepositoryPath)

		requiresTypes = [ 'BUILD_REQUIRES' ]
		packageInfoFiles = self._generatePackageInfoFiles(requiresTypes,
											 			  workRepositoryPath)
		try:
			self._resolveDependencies(packageInfoFiles, [], 
									  'why is port needed',
									  buildPlatform.isHaiku(),
									  [ workRepositoryPath ])
		except SystemExit:
			return

		requiresTypes = [ 'BUILD_PREREQUIRES', 'SCRIPTLET_PREREQUIRES' ]
		packageInfoFiles = self._generatePackageInfoFiles(requiresTypes,
														  workRepositoryPath)
		try:
			self._resolveDependencies(packageInfoFiles, [],
									  'why is port needed', True,
									  [ workRepositoryPath ])
		except SystemExit:
			return

		warn("port %s doesn't seem to be required by %s"
			 % (requiredPort.versionedName, self.versionedName))

	def cleanWorkDirectory(self):
		"""Clean the working directory"""

		if os.path.exists(self.workDir):
			print 'Cleaning work directory of %s ...' % self.versionedName
			shutil.rmtree(self.workDir)

	def downloadSource(self):
		"""Fetch the source archives and validate their checksum"""

		self.parseRecipeFileIfNeeded()
		for source in self.sources:
			source.fetch(self)
			source.validateChecksum(self)

	def unpackSource(self):
		"""Unpack the source archive(s)"""

		self.parseRecipeFileIfNeeded()
		for source in self.sources:
			source.unpack(self)

	def patchSource(self):
		"""Apply the Haiku patches to the source(s)"""

		self.parseRecipeFileIfNeeded()

		# skip all patches if any of the sources comes from a rigged source
		# package (as those contain already patched sources)
		for source in self.sources:
			if source.isFromRiggedSourcePackage():
				return

		patched = False
		for source in self.sources:
			if source.patch(self):
				patched = True

		# Run PATCH() function in recipe, if defined.
		if Phase.PATCH in self.definedPhases:
			if getOption('patchFilesOnly'):
				print 'Skipping patch function ...'
				return

			# Check to see if the patching phase has already been executed.
			if self.checkFlag('patch') and not getOption('force'):
				return

			try:
				print 'Running patch function ...'
				self._doRecipeAction(Phase.PATCH, self.sourceDir)
				for source in self.sources:
					source.commitPatchPhase()
				self.setFlag('patch')
			except:
				# Don't leave behind half-patched sources.
				if patched:
					for source in self.sources:
						source.reset()
				raise

	def extractPatchset(self):
		"""Extract patchsets from all sources"""

		self.parseRecipeFileIfNeeded()
		if self.isMetaPort:
			return

		s = 1
		for source in self.sources:
			if s == 1:
				patchSetFileName = self.name + '-' + self.version + '.patchset'
				archPatchSetFileName = (self.name + '-' + self.version + '-'
										+ self.targetArchitecture
										+ '.patchset')
			else:
				patchSetFileName = (self.name + '-' + self.version + '-source'
									+ str(s) + '.patchset')
				archPatchSetFileName = (self.name + '-' + self.version + '-'
										+ self.targetArchitecture + '-source'
										+ str(s) + '.patchset')
			patchSetFilePath = self.patchesDir + '/' + patchSetFileName
			archPatchSetFilePath = self.patchesDir + '/' + archPatchSetFileName
			source.extractPatchset(patchSetFilePath, archPatchSetFilePath)
			s += 1

	def build(self, packagesPath, makePackages, hpkgStoragePath):
		"""Build the port and collect the resulting package(s)"""

		self.parseRecipeFileIfNeeded()

		# reset build flag if recipe is newer (unless that's prohibited)
		if (not getOption('preserveFlags') and self.checkFlag('build')
			and (os.path.getmtime(self.recipeFilePath)
				 > os.path.getmtime(self.workDir + '/flag.build'))):
			print 'unsetting build flag, as recipe is newer'
			self.unsetFlag('build')

		self._recreatePackageDirectories()

		for package in self.packages:
			if ((getOption('createSourcePackagesForBootstrap')
					or getOption('createSourcePackages'))
				and package.type != PackageType.SOURCE):
				continue
			os.mkdir(package.packagingDir)
			package.prepopulatePackagingDir(self)

		if (getOption('createSourcePackagesForBootstrap')
			or getOption('createSourcePackages')):
			requiredPackages = []
			prerequiredPackages = []
		else:
			requiredPackages = self._getPackagesRequiredForBuild(packagesPath)
			prerequiredPackages \
				= self._getPackagesPrerequiredForBuild(packagesPath)
			self.requiresUpdater \
				= RequiresUpdater(self.packages, requiredPackages)
			if not Configuration.isCrossBuildRepository():
				self.requiresUpdater.addPackages(
					buildPlatform.findDirectory('B_SYSTEM_PACKAGES_DIRECTORY'))
		self.policy.setPort(self, requiredPackages)

		allPackages = set(requiredPackages + prerequiredPackages)
		if buildPlatform.usesChroot():
			# setup chroot and keep it while executing the actions
			chrootEnvVars = {
				'packages': '\n'.join(allPackages),
				'recipeFile': self.preparedRecipeFile,
				'targetArchitecture': self.targetArchitecture,
				'portDir': self.baseDir,
			}
			if Configuration.isCrossBuildRepository():
				chrootEnvVars['crossSysrootDir'] \
					= self.shellVariables['crossSysrootDir']
			
			def makeChrootFunctions():
				def taskFunction():
					if not getOption('quiet'):
						print 'chroot has these packages active:'
						for package in sorted(allPackages):
							print '\t' + package
					if getOption('enterChroot'):
						self._openShell()
					else:
						self._executeBuild(makePackages)
				def successFunction():
					# tell the shell scriptlets that the task has succeeded
					chrootSetup.buildOk = True
				def failureFunction():
					sysExit('Build has failed - stopping.')
				return {
					'task': taskFunction, 
					'success': successFunction, 
					'failure': failureFunction 
				}
			with ChrootSetup(self.workDir, chrootEnvVars) as chrootSetup:
				self._executeInChroot(chrootSetup, makeChrootFunctions())
		else:
			if not getOption('quiet'):
				print 'non-chroot has these packages active:'
				for package in sorted(allPackages):
					print '\t' + package

			buildPlatform.setupNonChrootBuildEnvironment(self.workDir,
				self.secondaryArchitecture, allPackages)
			try:
				self._executeBuild(makePackages)
			except:
				buildPlatform.cleanNonChrootBuildEnvironment(self.workDir,
					self.secondaryArchitecture, False)
				raise
			buildPlatform.cleanNonChrootBuildEnvironment(self.workDir,
				self.secondaryArchitecture, True)

		if makePackages and not getOption('enterChroot'):
			# move all created packages into packages folder
			for package in self.packages:
				if ((getOption('createSourcePackagesForBootstrap')
						or getOption('createSourcePackages'))
					and package.type != PackageType.SOURCE):
					continue
				packageFile = self.hpkgDir + '/' + package.hpkgName
				if os.path.exists(packageFile):
					if not (buildPlatform.usesChroot()
						or Configuration.isCrossBuildRepository()
						or getOption('createSourcePackagesForBootstrap')
						or getOption('createSourcePackages')):
						warn('not grabbing ' + package.hpkgName
							 + ', as it has not been built in a chroot.')
						continue
					print('grabbing ' + package.hpkgName
						  + ' and putting it into ' + hpkgStoragePath)
					os.rename(packageFile,
							  hpkgStoragePath + '/' + package.hpkgName)

		if os.path.exists(self.hpkgDir):
			os.rmdir(self.hpkgDir)

	def test(self, packagesPath):
		"""Test the port"""

		if not buildPlatform.isHaiku():
			sysExit("Sorry, can't execute a test unless running on Haiku")
			
		self.parseRecipeFileIfNeeded()

		self._recreatePackageDirectories()

		requiredPackages = self._getPackagesRequiredForBuild(packagesPath)
		prerequiredPackages \
				= self._getPackagesPrerequiredForBuild(packagesPath)
		self.policy.setPort(self, requiredPackages)

		allPackages = set(requiredPackages + prerequiredPackages)
		# setup chroot and keep it while executing the actions
		chrootEnvVars = {
			'packages': '\n'.join(allPackages),
			'recipeFile': self.preparedRecipeFile,
			'targetArchitecture': self.targetArchitecture,
			'portDir': self.baseDir,
		}

		def makeChrootFunctions():
			def taskFunction():
				self._executeTest()
			def failureFunction():
				sysExit('Test has failed - stopping.')
			return {
				'task': taskFunction, 
				'failure': failureFunction 
			}
		with ChrootSetup(self.workDir, chrootEnvVars) as chrootSetup:
			self._executeInChroot(chrootSetup, makeChrootFunctions())

	def setFlag(self, name, index = '1'):
		if index == '1':
			touchFile('%s/flag.%s' % (self.workDir, name))
		else:
			touchFile('%s/flag.%s-%s' % (self.workDir, name, index))

	def unsetFlag(self, name, index = '1'):
		if index == '1':
			flagFile = '%s/flag.%s' % (self.workDir, name)
		else:
			flagFile = '%s/flag.%s-%s' % (self.workDir, name, index)

		if os.path.exists(flagFile):
			os.remove(flagFile)

	def checkFlag(self, name, index = '1'):
		if index == '1':
			return os.path.exists('%s/flag.%s' % (self.workDir, name))

		return os.path.exists('%s/flag.%s-%s' % (self.workDir, name, index))

	def _parseRecipeFile(self, showWarnings):
		"""Parse the recipe-file of the specified port"""

		# temporarily mark the recipe as broken, such that any exception
		# will leave this marker on
		self.recipeIsBroken = True

		# set default SOURCE_DIR
		self.shellVariables['SOURCE_DIR'] = self.baseName + '-' + self.version

		self.recipeKeysByExtension = self.validateRecipeFile(showWarnings)
		self.recipeKeys = {}
		for entries in self.recipeKeysByExtension.values():
			self.recipeKeys.update(entries)

		# initialize variables that depend on the recipe revision
		self.revision = str(self.recipeKeys['REVISION'])
		self.fullVersion = self.version + '-' + self.revision
		self.revisionedName = self.name + '-' + self.fullVersion

		# create sources
		self.sources = []
		keys = self.recipeKeys
		basedOnSourcePackage = False
		for index in sorted(keys['SRC_URI'].keys(), cmp=naturalCompare):
			source = Source(self, index, keys['SRC_URI'][index],
							keys['SRC_FILENAME'].get(index, None),
							keys['CHECKSUM_MD5'].get(index, None),
							keys['SOURCE_DIR'].get(index, None),
							keys['PATCHES'].get(index, []),
							keys['ADDITIONAL_FILES'].get(index, []))
			if source.isFromSourcePackage():
				basedOnSourcePackage = True
			self.sources.append(source)

		# create packages
		self.allPackages = []
		self.packages = []
		haveSourcePackage = False
		for extension in sorted(self.recipeKeysByExtension.keys()):
			keys = self.recipeKeysByExtension[extension]
			if extension:
				name = self.name + '_' + extension
			else:
				name = self.name
			packageType = PackageType.byName(extension)
			package = packageFactory(packageType, name, self, keys, self.policy)
			self.allPackages.append(package)

			if packageType == PackageType.SOURCE:
				if getOption('noSourcePackages') or basedOnSourcePackage:
					# creation of the source package should be avoided, so we
					# skip adding it to the list of active packages
					continue
				haveSourcePackage = True

			if package.isBuildableOnArchitecture(self.targetArchitecture):
				self.packages.append(package)

		if not self.isMetaPort:
			# create source package if it hasn't been specified or disabled:
			if (not haveSourcePackage and not keys['DISABLE_SOURCE_PACKAGE']
				and not basedOnSourcePackage
				and not getOption('noSourcePackages')):
				package = self._createSourcePackage(name, False)
				self.allPackages.append(package)
				self.packages.append(package)

			# create additional rigged source package if necessary
			if getOption('createSourcePackagesForBootstrap'):
				package = self._createSourcePackage(name, True)
				self.allPackages.append(package)
				self.packages.append(package)

		if self.sources:
			self.sourceDir = self.sources[0].sourceDir
		else:
			self.sourceDir = self.workDir

		# set up the complete list of variables we'll inherit to the shell
		# when executing a recipe action
		self._updateShellVariablesFromRecipe()

		# take notice that the recipe is ok
		self.recipeIsBroken = False

	def _updateShellVariablesFromRecipe(self):
		"""Fill dictionary with variables that will be inherited to the shell
		   when executing recipe actions
		"""
		self._updateShellVariables(False)

	def _updateShellVariables(self, forParsing):
		"""Fill dictionary with variables that will be inherited to the shell
		   when executing recipe actions repectively for parsing the recipe.
		   If forParsing is True, only a subset of variables is set and some
		   others need reevaluation in the shell script after the revision is
		   known.
		"""
		if forParsing:
			revision = '$REVISION'
			fullVersion = self.version + '-' + revision
			revisionedName = self.name + '-' + fullVersion
		else:
			revision = self.revision
			fullVersion = self.fullVersion
			revisionedName = self.revisionedName

		self.shellVariables.update({
			'portRevision': revision,
			'portFullVersion': fullVersion,
			'portRevisionedName': revisionedName,
			'portDir': self.workDir + '/port',
		})

		if not forParsing:
			for source in self.sources:
				if source.index == '1':
					sourceDirKey = 'sourceDir'
				else:
					sourceDirKey = 'sourceDir' + source.index
				self.shellVariables[sourceDirKey] = source.sourceDir
			self.shellVariables.update({
				'packagingBaseDir': self.packagingBaseDir,
				'workDir': self.workDir,
			})

		if self.secondaryArchitecture:
			secondaryArchSubDir = '/' + self.secondaryArchitecture
			# don't use a subdir when building cross-packages
			if (Configuration.isCrossBuildRepository()
				and '_cross_' in self.name):
				secondaryArchSubDir = ''
			secondaryArchSuffix = '_' + self.secondaryArchitecture
		else:
			secondaryArchSubDir = ''
			secondaryArchSuffix = ''

		effectiveTargetMachineTriple = MachineArchitecture.getTripleFor(
			self.effectiveTargetArchitecture)

		self.shellVariables['secondaryArchSubDir'] = secondaryArchSubDir
		self.shellVariables['secondaryArchSuffix'] = secondaryArchSuffix

		self.shellVariables['effectiveTargetArchitecture'] \
			= self.effectiveTargetArchitecture
		self.shellVariables['effectiveTargetMachineTriple'] \
			= effectiveTargetMachineTriple
		self.shellVariables['effectiveTargetMachineTripleAsName'] \
			= effectiveTargetMachineTriple.replace('-', '_')

		basePrefix = ''
		if Configuration.isCrossBuildRepository():
			# If this is a cross package, we possibly want to use an additional
			# base prefix. Otherwise the prefix is as if building natively on
			# Haiku, but we may need to prepend a dest-dir in the install phase
			# (when we don't use a chroot).
			if '_cross_' in self.name:
				basePrefix = \
					buildPlatform.getCrossToolsBasePrefix(self.workDir)
			else:
				installDestDir = buildPlatform.getInstallDestDir(self.workDir)
				if installDestDir:
					self.shellVariables['installDestDir'] = installDestDir

			self.shellVariables['crossSysrootDir'] \
				= buildPlatform.getCrossSysrootDirectory(self.workDir)

		relativeConfigureDirs = {
			'dataDir':			'data',
			'dataRootDir':		'data',
			'binDir':			'bin' + secondaryArchSubDir,
			'sbinDir':			'bin' + secondaryArchSubDir,
			'libDir':			'lib' + secondaryArchSubDir,
			'includeDir':		'develop/headers' + secondaryArchSubDir,
			'oldIncludeDir':	'develop/headers' + secondaryArchSubDir,
			'docDir':			'documentation/packages/' + self.name,
			'infoDir':			'documentation/info',
			'manDir':			'documentation/man',
			'libExecDir':		'lib',
			'sharedStateDir':	'var',
			'localStateDir':	'var',
			# sysconfdir is only defined in configDirs below, since it is not
			# necessarily below prefix
		}

		# Note: Newer build systems also support the following options. Their
		# default values are OK for us for now:
		# --localedir=DIR         locale-dependent data [DATAROOTDIR/locale]
		# --htmldir=DIR           html documentation [DOCDIR]
		# --dvidir=DIR            dvi documentation [DOCDIR]
		# --pdfdir=DIR            pdf documentation [DOCDIR]
		# --psdir=DIR             ps documentation [DOCDIR]

		portPackageLinksDir = (basePrefix
			+ buildPlatform.findDirectory('B_PACKAGE_LINKS_DIRECTORY')
			+ '/' + revisionedName)
		self.shellVariables['portPackageLinksDir'] = portPackageLinksDir

		prefix = portPackageLinksDir + '/.self'

		configureDirs = {
			'prefix':		prefix,
			'sysconfDir':	portPackageLinksDir + '/.settings',
		}

		for name, value in relativeConfigureDirs.iteritems():
			relativeName = 'relative' + name[0].upper() + name[1:]
			self.shellVariables[relativeName] = value
			configureDirs[name] = prefix + '/' + value

		self.shellVariables.update(configureDirs)

		# add one more variable containing all the dir args for configure:
		self.shellVariables['configureDirArgs'] \
			= ' '.join('--%s=%s' % (k.lower(), v)
					   for k, v in configureDirs.iteritems())

		# add another one with the list of possible variables
		self.shellVariables['configureDirVariables'] \
			= ' '.join(configureDirs.iterkeys())

		# Add variables for other standard directories. Consequently, we should
		# use finddir to get them (also for the configure variables above), but
		# we want relative paths here.
		relativeOtherDirs = {
			'addOnsDir':		'add-ons' + secondaryArchSubDir,
			'appsDir':			'apps',
			'debugInfoDir':		'develop/debug',
			'developDir':		'develop',
			'developDocDir':	'develop/documentation/'  + self.name,
			'developLibDir':	'develop/lib' + secondaryArchSubDir,
			'documentationDir':	'documentation',
			'fontsDir':			'data/fonts',
			'postInstallDir':	'boot/post-install',
			'preferencesDir':	'preferences',
			'settingsDir':		'settings',
		}

		for name, value in relativeOtherDirs.iteritems():
			relativeName = 'relative' + name[0].upper() + name[1:]
			self.shellVariables[relativeName] = value
			self.shellVariables[name] = prefix + '/' + value

	def _recreatePackageDirectories(self):
		# Delete and re-create a couple of directories
		directoriesToCreate = [
			self.packageInfoDir, self.packagingBaseDir,
			self.buildPackageDir, self.hpkgDir
		]
		directoriesToRemove = [
			directory for directory in directoriesToCreate
			if os.path.exists(directory)
		]
		if directoriesToRemove:
			print 'Cleaning up temporary directories ...'
			for directory in directoriesToRemove:
				shutil.rmtree(directory, True)
		for directory in directoriesToCreate:
			os.mkdir(directory)

	def _executeInChroot(self, chrootSetup, chrootFunctions):
		pid = os.fork()
		if pid == 0:
			# child, enter chroot and execute the given task
			try:
				os.chroot(self.workDir)
				self._adjustToChroot()
				chrootFunctions['task']()
			except BaseException as exception:
				if not getOption('enterChroot'):
					if getOption('debug'):
						traceback.print_exc()
					else:
						print exception
					os._exit(1)
			os._exit(0)

		# parent, wait on child
		try:
			childStatus = os.waitpid(pid, 0)[1]
			if not getOption('enterChroot'):
				if childStatus != 0:
					if 'failure' in chrootFunctions:
						chrootFunctions['failure']()
					# normally, the following should never be executed,
					# as the error function is meant to return.
					sysExit('chroot-task failed')
				if 'success' in chrootFunctions:
					chrootFunctions['success']()
		except KeyboardInterrupt:
			if pid > 0:
				print '*** interrupted - stopping child process'
				try:
					os.kill(pid, signal.SIGINT)
					os.waitpid(pid, 0)
				except:
					pass
				print '*** child stopped'

	def _generatePackageInfoFiles(self, requiresTypes, path = None):
		"""Generates package info files with given types of requires."""

		if not path:
			path = self.packageInfoDir
		if not os.path.exists(path):
			os.makedirs(path)

		packageInfoFiles = []

		for package in self.packages:
			packageInfoFile = (path + '/' + package.packageInfoName)
			package.generatePackageInfoWithoutProvides(packageInfoFile,
													   requiresTypes)
			packageInfoFiles.append(packageInfoFile)

		return packageInfoFiles

	def _getPackagesPrerequiredForBuild(self, packagesPath):
		"""Determine the set of prerequired packages that must be linked into
		   the build environment (chroot) for the build stage"""

		requiresTypes = [ 'BUILD_PREREQUIRES', 'SCRIPTLET_PREREQUIRES' ]
		packageInfoFiles = self._generatePackageInfoFiles(requiresTypes)

		prereqPackages = self._resolveDependencies(
			packageInfoFiles, [ packagesPath ],
			'prerequired packages for build', True)

		return prereqPackages

	def _getPackagesRequiredForBuild(self, packagesPath):
		"""Determine the set of packages that must be linked into the
		   build environment (chroot) for the build stage"""

		requiresTypes = [ 'BUILD_REQUIRES' ]
		packageInfoFiles = self._generatePackageInfoFiles(requiresTypes)

		packages = self._resolveDependencies(packageInfoFiles, 
											 [ packagesPath ], 
											 'required packages for build',
											 buildPlatform.isHaiku())

		return packages

	def _executeBuild(self, makePackages):
		"""Executes the build stage and creates all declared packages"""

		self._createBuildPackages()

		if not (getOption('createSourcePackagesForBootstrap')
			or getOption('createSourcePackages')):
			try:
				self._doBuildStage()
			except BaseException:
				self.unsetFlag('build')
				raise

		if makePackages:
			self._makePackages()

		self._removeBuildPackages()

	def _executeTest(self):
		"""Executes the test stage"""

		self._createBuildPackages()

		self._doTestStage()

		self._removeBuildPackages()

	def _createBuildPackages(self):
		# create all build packages (but don't activate them yet)
		for package in self.packages:
			if ((getOption('createSourcePackagesForBootstrap')
					or getOption('createSourcePackages'))
				and package.type != PackageType.SOURCE):
				continue
			package.createBuildPackage()

	def _removeBuildPackages(self):
		for package in self.packages:
			if ((getOption('createSourcePackagesForBootstrap')
					or getOption('createSourcePackages'))
				and package.type != PackageType.SOURCE):
				continue
			package.removeBuildPackage()

	def _adjustToChroot(self):
		"""Adjust directories to chroot()-ed environment"""

		for source in self.sources:
			source.adjustToChroot(self)

		for package in self.allPackages:
			package.adjustToChroot()

		# unset directories which can't be reached from inside the chroot
		self.baseDir = None
		self.downloadDir = None
		self.outputDirectory = None

		# the recipe file has a fixed same name in the chroot
		self.preparedRecipeFile = '/port.recipe'
		self.recipeFilePath = '/port.recipe'

		# adjust all relevant directories
		pathLengthToCut = len(self.workDir)
		self.sourceDir = self.sourceDir[pathLengthToCut:]
		self.sourceBaseDir = self.sourceBaseDir[pathLengthToCut:]
		self.buildPackageDir = self.buildPackageDir[pathLengthToCut:]
		self.packagingBaseDir = self.packagingBaseDir[pathLengthToCut:]
		self.packageInfoDir = self.packageInfoDir[pathLengthToCut:]
		self.hpkgDir = self.hpkgDir[pathLengthToCut:]
		self.workDir = ''

		if not self.isMetaPort:
			self.patchesDir = '/patches'
			self.licensesDir = '/licenses'

		# update shell variables, too
		self._updateShellVariablesFromRecipe()


	def _doBuildStage(self):
		"""Run the actual build"""
		# activate build package if required at this stage
		if self.recipeKeys['BUILD_PACKAGE_ACTIVATION_PHASE'] == Phase.BUILD:
			for package in self.packages:
				package.activateBuildPackage()

		# Check to see if a previous build was already done.
		if self.checkFlag('build') and not getOption('force'):
			print 'Skipping build ...'
			return

		print 'Building ...'
		self._doRecipeAction(Phase.BUILD, self.sourceDir)
		self.setFlag('build')

	def _makePackages(self):
		"""Create all packages suitable for distribution"""

		if not (getOption('createSourcePackagesForBootstrap')
			or getOption('createSourcePackages')):
			# Create the settings directory in the packaging directory, if
			# needed. We need to do that, since the .settings link would
			# otherwise point to a non-existing entry and the directory
			# couldn't be made either.
			for package in self.packages:
				settingsDir = package.packagingDir + '/settings'
				if not os.path.exists(settingsDir):
					os.makedirs(settingsDir)

			self._doInstallStage()

			# If the settings directory is still empty, remove it.
			for package in self.packages:
				settingsDir = package.packagingDir + '/settings'
				if not os.listdir(settingsDir):
					os.rmdir(settingsDir)

			# For secondary architecture packages symlink the bin/<arch>/*
			# entries to bin/ with a respective suffix.
			if self.secondaryArchitecture:
				for package in self.packages:
					binDir = package.packagingDir + '/bin'
					archBinDir = binDir + '/' + self.secondaryArchitecture
					if os.path.exists(archBinDir):
						for entry in os.listdir(archBinDir):
							os.symlink(self.secondaryArchitecture + '/' + entry,
								binDir + '/' + entry + '-'
									+ self.secondaryArchitecture)

			# For the main package remove certain empty directories. Typically
			# contents is moved from the main package installation directory
			# tree to the packaging directories of sibling packages, which may
			# leave empty directories behind.
			for dirName in [ 'add-ons', 'apps', 'bin', 'data', 'develop',
					'documentation', 'lib', 'preferences' ]:
				dir = self.packagingBaseDir + '/' + self.name + '/' + dirName
				if os.path.exists(dir) and not os.listdir(dir):
					os.rmdir(dir)

		# create hpkg-directory if needed
		if not os.path.exists(self.hpkgDir):
			os.makedirs(self.hpkgDir)

		# make each package
		for package in self.packages:
			if ((getOption('createSourcePackagesForBootstrap')
					or getOption('createSourcePackages'))
				and package.type != PackageType.SOURCE):
				continue
			package.makeHpkg(self.requiresUpdater)

		# Clean up after ourselves
		shutil.rmtree(self.packagingBaseDir)

	def _doInstallStage(self):
		"""Install the files resulting from the build into the packaging
		   folder"""

		# activate build package if required at this stage
		if self.recipeKeys['BUILD_PACKAGE_ACTIVATION_PHASE'] == Phase.INSTALL:
			for package in self.packages:
				package.activateBuildPackage()

		print 'Collecting files to be packaged ...'
		self._doRecipeAction(Phase.INSTALL, self.sourceDir)

	def _doTestStage(self):
		"""Test the build results"""

		# activate build package if required at this stage
		if self.recipeKeys['BUILD_PACKAGE_ACTIVATION_PHASE'] == Phase.TEST:
			for package in self.packages:
				package.activateBuildPackage()

		print 'Testing ...'
		self._doRecipeAction(Phase.TEST, self.sourceDir)

	def _doRecipeAction(self, action, dir):
		"""Run the specified action, as defined in the recipe file"""

		# execute the requested action via a shell ...
		shellVariables = self.shellVariables.copy()
		shellVariables['fileToParse'] = self.preparedRecipeFile
		shellVariables['recipeAction'] = action
		wrapperScriptContent = (getShellVariableSetters(shellVariables)
			+ recipeActionScript)
		wrapperScript = self.workDir + '/wrapper-script'
		storeStringInFile(wrapperScriptContent, wrapperScript)
		self._openShell(['-c', '. ' + wrapperScript], dir)

	def _openShell(self, params = [], dir = '/'):
		"""Sets up environment and runs a shell with the given parameters"""

		# set up the shell environment -- we want it to inherit some of our
		# variables
		shellEnv = filteredEnvironment()
		if Configuration.isCrossBuildRepository():
			# include cross development tools in path automatically
			crossToolsPaths = buildPlatform.getCrossToolsBinPaths(self.workDir)
			shellEnv['PATH'] = ':'.join(crossToolsPaths + [ shellEnv['PATH'] ])
		elif self.secondaryArchitecture:
			# include secondary architecture tools in path
			secondaryArchPaths = [
				'/boot/system/bin/' + self.secondaryArchitecture ]
			shellEnv['PATH'] = ':'.join(
				secondaryArchPaths + [ shellEnv['PATH'] ])

		# Request scripting language (perl, python) modules to be installed
		# into vendor directories automatically.
		shellEnv['HAIKU_USE_VENDOR_DIRECTORIES'] = '1';

		# force POSIX locale, as otherwise strange things may happen for some
		# build (e.g. gcc)
		shellEnv['LC_ALL'] = 'POSIX'

		# execute the requested action via a shell ...
		args = [ '/bin/bash' ]
		args += params
		check_call(args, cwd=dir, env=shellEnv)

	def _resolveDependencies(self, packageInfoFiles, repositories, description,
			considerBuildhostPackages, fallbackRepositories = []):
		"""Resolve dependencies of one or more package-infos"""

		try:
			return buildPlatform.resolveDependencies(packageInfoFiles,
				repositories, considerBuildhostPackages, fallbackRepositories)
		except (CalledProcessError, LookupError):
			sysExit(('unable to resolve %s for %s\n'
					 + '\tpackage-infos:\n\t\t%s\n'
					 + '\trepositories:\n\t\t%s\n')
					% (description, self.versionedName,
					   '\n\t\t'.join(packageInfoFiles),
					   '\n\t\t'.join(repositories)))

	def _createSourcePackage(self, name, rigged):
		# copy all recipe attributes from base package, but set defaults
		# for everything that's package-specific:
		sourceKeys = {}
		baseKeys = self.recipeKeysByExtension['']
		recipeAttributes = getRecipeAttributes()
		for key in baseKeys.keys():
			if recipeAttributes[key]['extendable'] != Extendable.NO:
				sourceKeys[key] = recipeAttributes[key]['default']
			else:
				sourceKeys[key] = baseKeys[key]

		# a source package shares some attributes with the base package,
		# just provides itself and has no requires:
		sourceSuffix \
			= recipeAttributes['SUMMARY']['suffix'][PackageType.SOURCE]
		sourceKeys.update({
			'ARCHITECTURES': baseKeys['ARCHITECTURES'],
			'COPYRIGHT': baseKeys['COPYRIGHT'],
			'DESCRIPTION': baseKeys['DESCRIPTION'],
			'HOMEPAGE': baseKeys['HOMEPAGE'],
			'LICENSE': baseKeys['LICENSE'],
			'PROVIDES': [ name + ' = ' + self.version ],
			'SUMMARY': (baseKeys['SUMMARY'] + sourceSuffix),
		})
		if rigged:
			name = self.name + '_source_rigged'
		else:
			name = self.name + '_source'
		return sourcePackageFactory(name, self, sourceKeys, self.policy, rigged)