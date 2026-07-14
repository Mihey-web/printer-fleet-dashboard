function getStateColors() {
        var style = getComputedStyle(document.documentElement);
        return {
          printing: style.getPropertyValue('--info').trim(),
          finished: style.getPropertyValue('--ok').trim(),
          paused: style.getPropertyValue('--warn').trim(),
          error: style.getPropertyValue('--danger').trim(),
          idle: style.getPropertyValue('--gray-11').trim(),
          offline: style.getPropertyValue('--gray-7').trim(),
          unknown: style.getPropertyValue('--gray-11').trim()
        };
      }
function getTimelineColors() {
        var style = getComputedStyle(document.documentElement);
        return {
          bgTop: style.getPropertyValue('--tl-bg-top').trim(),
          bgBottom: style.getPropertyValue('--tl-bg-bottom').trim(),
          zebra: style.getPropertyValue('--tl-zebra').trim(),
          gridMajor: style.getPropertyValue('--tl-grid-major').trim(),
          gridMinor: style.getPropertyValue('--tl-grid-minor').trim(),
          axisLine: style.getPropertyValue('--tl-axis-line').trim(),
          axisText: style.getPropertyValue('--tl-axis-text').trim(),
          tickMajor: style.getPropertyValue('--tl-tick-major').trim(),
          tickMinor: style.getPropertyValue('--tl-tick-minor').trim(),
          labelText: style.getPropertyValue('--tl-label-text').trim(),
          labelDot: style.getPropertyValue('--tl-label-dot').trim(),
          barShadow: style.getPropertyValue('--tl-bar-shadow').trim(),
          barHighlight: style.getPropertyValue('--tl-bar-highlight').trim(),
          nowLine: style.getPropertyValue('--tl-now-line').trim(),
          nowText: style.getPropertyValue('--tl-now-text').trim()
        };
      }