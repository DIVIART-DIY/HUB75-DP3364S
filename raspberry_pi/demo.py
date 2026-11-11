"""Standalone DP3364S demo. Default: offline checks; --run enables hardware."""
import argparse,json,signal,time
import dp3364s_blue as panel
import dpi_transport as transport
from panel_frame import Encoder,NativeEncoder
from drm_presenter import Presenter
from patterns import picture,COLOURS
PIXEL_HZ=16000000
HEIGHT=514
FPS=PIXEL_HZ/((panel.WIDTH+3)*(HEIGHT+3))
class DemoPresenter(Presenter):
    def __init__(self,encoder,pattern,colour):
        super().__init__([encoder.template]);self.encoder=encoder;self.pattern=pattern;self.colour=colour
    def run(self,lib,fd,crtc,initial_fb,initial_mapping,initial_pitch,seconds):
        self.prepare(lib,fd,crtc,initial_fb,initial_mapping,initial_pitch)
        start=time.monotonic();frame=0
        while time.monotonic()-start<seconds:
            self.present(self.encoder.encode(picture(self.pattern,self.colour,frame)))
            frame+=1 # advance after acknowledged presentation, never by elapsed wall time
        return dict(flips_acknowledged=self.completed,updates_per_second=frame/(time.monotonic()-start))
    def blank_before_stop(self):
        signal.signal(signal.SIGTERM,signal.SIG_IGN);signal.signal(signal.SIGINT,signal.SIG_IGN)
        try:
            if self.lib is not None and hasattr(self,'buffers'):
                self.present(self.encoder.template);time.sleep(3/FPS)
        finally:panel.hold_row_disabled()
def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',action='store_true');p.add_argument('--test-boot',action='store_true');p.add_argument('--dp3364s-connected',action='store_true')
    p.add_argument('--pattern',choices=('solid','geometry','gradients','motion'),default='solid')
    p.add_argument('--colour',choices=tuple(COLOURS),default='blue');p.add_argument('--seconds',type=int,choices=range(1,301),default=30)
    args=p.parse_args()
    if not args.run:
        encoder=Encoder(full_pwm_cycle=True)
        assert len(encoder.template)==1288*514*4
        for name in ('solid','geometry','gradients','motion'):
            rgb=picture(name,args.colour);assert len(rgb)==160*80*3;assert len(encoder.encode(rgb))==len(encoder.template)
        print(json.dumps(dict(status='PASS',hardware_access=False,full_pwm_groups=64,rows=40,dpi_size=[1288,514],requested_pixel_hz=PIXEL_HZ,requested_active_dclk_hz=PIXEL_HZ//2,nominal_refresh_hz=FPS)))
        return
    if not args.test_boot or not args.dp3364s_connected:raise SystemExit('Require --test-boot --dp3364s-connected')
    transport.guard_configuration(args)
    encoder=NativeEncoder(max_level=0x0999,full_pwm_cycle=True)
    presenter=DemoPresenter(encoder,args.pattern,args.colour)
    transport.WIDTH,transport.HEIGHT=panel.WIDTH,HEIGHT
    mode=transport.Mode(clock=16000,hdisplay=panel.WIDTH,hsync_start=panel.WIDTH+1,hsync_end=panel.WIDTH+2,htotal=panel.WIDTH+3,vdisplay=HEIGHT,vsync_start=HEIGHT+1,vsync_end=HEIGHT+2,vtotal=HEIGHT+3,flags=5,type=32,name=b'dp3364s-demo')
    args.device,args.timing='/dev/dri/card0','full-pwm'
    def stop(signum,frame):
        signal.signal(signal.SIGTERM,signal.SIG_IGN);signal.signal(signal.SIGINT,signal.SIG_IGN);raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    transport.run(args,mode_override=mode,frame_pixels=encoder.template,
        before_enable=lambda:panel.initialise(hold_blank=True),after_enable=panel.release_row_to_dma,
        before_disable=presenter.blank_before_stop,after_disable=lambda:panel.blank_after_stop(hold_blank=True),
        safety_check=transport.guard_configuration,presenter=presenter)
if __name__=='__main__':main()
