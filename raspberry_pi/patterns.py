"""RGB test pictures. Pure Python; no hardware access."""
WIDTH, HEIGHT = 160, 80
COLOURS = {'red':(255,0,0),'green':(0,255,0),'blue':(0,0,255),
           'white':(255,255,255),'yellow':(255,255,0),'cyan':(0,255,255),
           'magenta':(255,0,255),'black':(0,0,0)}
GLYPHS = {'H': (17, 17, 17, 31, 17, 17, 17), 'U': (17, 17, 17, 17, 17, 17, 14), 'B': (30, 17, 17, 30, 17, 17, 30), '7': (31, 1, 2, 4, 8, 8, 8), '5': (31, 16, 16, 30, 1, 1, 30), 'D': (30, 17, 17, 17, 17, 17, 30), 'M': (17, 27, 21, 21, 17, 17, 17), 'A': (14, 17, 17, 31, 17, 17, 17)}
def picture(pattern='solid',colour='blue',frame=0):
    rgb=bytearray(WIDTH*HEIGHT*3)
    def put(x,y,c):
        if 1<=x<=159 and 1<=y<=78:
            pos=(y*WIDTH+x)*3;rgb[pos:pos+3]=bytes(c)
    if pattern=='solid':
        for y in range(1,79):
            for x in range(1,160):put(x,y,COLOURS[colour])
    elif pattern=='geometry':
        for x in range(1,160):put(x,1,COLOURS['white']);put(x,78,COLOURS['white'])
        for y in range(1,79):put(1,y,COLOURS['white']);put(159,y,COLOURS['white'])
        for x0,y0,c in ((3,3,'red'),(152,3,'green'),(3,72,'blue'),(152,72,'white')):
            for y in range(y0,y0+5):
                for x in range(x0,x0+5):put(x,y,COLOURS[c])
        for y in range(34,46):
            for x in (79,80):put(x,y,COLOURS['cyan'])
        for x in range(74,86):
            for y in (39,40):put(x,y,COLOURS['cyan'])
    elif pattern=='gradients':
        for y in range(1,79):
            band=min(3,(y-1)*4//78)
            for x in range(1,160):
                v=min(15,(x-1)*16//159)*17
                put(x,y,[(v,v,v),(v,0,0),(0,v,0),(0,0,v)][band])
    elif pattern=='motion':
        step=frame%316;x=1+min(step,316-step)
        for y in range(2,78):put(x,y,COLOURS['white'])
        for i,char in enumerate('HUB75 DMA'):
            for row,bits in enumerate(GLYPHS.get(char,(0,)*7)):
                for col in range(5):
                    if bits & (1<<(4-col)):
                        for dx in (0,1):
                            for dy in (0,1):put(208-frame%314+i*12+col*2+dx,33+row*2+dy,COLOURS['white'])
    else:raise ValueError('Unknown pattern')
    return rgb
