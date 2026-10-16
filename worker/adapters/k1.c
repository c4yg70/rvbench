#define _POSIX_C_SOURCE 200809L
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
extern void candidate(const float*, const float*, float*, int);
static double milliseconds(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return t.tv_sec*1000.0+t.tv_nsec/1e6; }
static uint32_t rng;
static float random_value(void) { rng = rng*1664525u+1013904223u; return (float)(rng>>8)/8388608.0f-1.0f; }
int main(int argc, char **argv) {
    if(argc!=7) return 2;
    int n=atoi(argv[2]), warmup=atoi(argv[4]), reps=atoi(argv[5]); rng=(uint32_t)strtoul(argv[6],0,10);
    if(n<1 || reps<1 || reps>1000 || warmup<0 || warmup>1000) return 2;
    float *a=malloc((size_t)n*4), *b=malloc((size_t)n*4), *out=malloc((size_t)n*4);
    if(!a||!b||!out) return 3;
    for(int i=0;i<n;i++){ a[i]=random_value(); b[i]=random_value(); out[i]=NAN; }
    candidate(a,b,out,n);
    if(!strcmp(argv[3],"func")){puts("{\"smoke\":true}"); return 0;}
    {
        double max=0; int correct=1;
        for(int i=0;i<n;i++){ float ref=!strcmp(argv[1],"add")?a[i]+b[i]:fabsf(a[i]); double e=fabs((double)out[i]-ref); if(!isfinite(out[i])||e>1e-6+1e-5*fabs(ref))correct=0; if(e>max)max=e; }
        if(!correct || !strcmp(argv[3],"correctness")) {
            printf("{\"correct\":%s,\"max_abs_error\":%.9g,\"reference\":\"native-c-scalar\",\"scope\":\"submitted-case\",\"atol\":0.000001,\"rtol\":0.00001}\n",correct?"true":"false",max);
            free(a);free(b);free(out);return 0;
        }
    }
    {
        for(int i=0;i<warmup;i++)candidate(a,b,out,n);
        printf("{\"samples_ms\":[");
        for(int i=0;i<reps;i++){ double start=milliseconds(); candidate(a,b,out,n); double dt=milliseconds()-start; printf("%s%.9g",i?",":"",dt); }
        printf("],\"correct\":true,\"reference\":\"native-c-scalar\",\"clock\":\"CLOCK_MONOTONIC\",\"warmup\":%d,\"repetitions\":%d,\"compilation_excluded\":true,\"checksum\":%.9g}\n",warmup,reps,(double)out[0]);
    }
    free(a);free(b);free(out);return 0;
}
