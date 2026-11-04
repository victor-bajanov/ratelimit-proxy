import Foundation

metricTests()
decodeTests()
sampleStoreTests()
valueFormatTests()
sparklineTests()
settingsTests()
pollOutcomeTests()
dualAxisTests()
axisFormatTests()

print("\(checkCount) checks, \(failureCount) failures")
exit(failureCount == 0 ? 0 : 1)
