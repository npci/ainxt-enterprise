// SPDX-License-Identifier: Apache-2.0
import ABStudioApp from '@abs/BuildStudio.jsx';

// This wrapper gives .build-studio-root a concrete height to resolve against.
//
// Height chain (embedded):
//   html/body/  #root  (host owns these — must have height:100% or h-screen)
//   └── App.jsx  "flex h-screen w-screen overflow-hidden"
//       └── content pane  "flex flex-col flex-1 min-w-0 h-full overflow-hidden"
//           └── <Route> renders this component
//               └── this div  (flex:1 + min-height:0 fills the pane)
//                   └── .build-studio-root  height:100%  ← resolves correctly
//
// flex:1 + min-height:0 is the correct pattern for a flex child that must
// fill its parent without overflowing it. height:100% alone can cause the
// child to be taller than the parent when the parent is a flex container.
export default function BuildStudio() {
  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      flex: '1 1 0%',
      minHeight: 0,
      minWidth: 0,
      width: '100%',
      overflow: 'hidden',
    }}>
      <ABStudioApp />
    </div>
  );
}
